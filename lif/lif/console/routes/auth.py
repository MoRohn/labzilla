"""Identity and access: /api/auth/*, /api/setup, /api/access, /api/pair/*, /api/devices/* (brief §3, spec §14–§17, §83).

Pairing lifecycle (§17), all transitions atomic (`UPDATE … WHERE status=?`, rowcount checked):
  waiting  — an admin POSTs /pair/start and shows <public_url>/pair#<token> as a QR. The token sits in
             the URL fragment (never in server logs) and only its sha256 is stored.
  claimed  — the phone POSTs /pair/claim {token, device_name}; it receives an HttpOnly `lz_pair` claim
             cookie and a 6-digit code = HMAC(token, claim secret). Admins get a `pairing` event and see
             the same code, so the person holding both screens can check they match. Single use.
  approved — an admin approves (devices.manage): a Device row is created for the admin who started it.
             The phone's next GET /pair/status atomically receives the device session cookie (once).
  rejected / expired — terminal. Each step has a pairing_ttl_sec window.
Same-device pairing is allowed (brief §3). Signing out of a paired device unpairs it: devices have
no passphrase, so the only way back in is a new pairing — which is what "sign out" should mean on a
phone left somewhere.

Exports for routes/agents.py (Approvals): `pending_approvals()` and `decide(pairing_id, approve, user)`.
Approval ids for pairings are "pairing:<id>".
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse

from lif.console import auth, db, errors, settings
from lif.console.auth import PAIR_COOKIE, SESSION_COOKIE
from lif.console.contracts import (AccessInfo, Approval, ApprovalOption, DetectedItem, Device, LoginRequest,
                                   OkResponse, PairClaimRequest, Pairing, SetupRequest, SetupState, TechDetail, User)
from lif.console.errors import human
from lif.console.events import hub

router = APIRouter(prefix="/api", tags=["auth"])     # spans areas: /auth, /setup, /access, /pair, /devices

PairStatus = Literal["waiting", "claimed", "approved", "rejected", "expired"]


def _db_error(e: sqlite3.Error) -> Exception:
    return errors.storage(e)


def _ua(request: Request) -> str:
    return request.headers.get("user-agent", "")[:300]


# ── who am I / sign in / sign out ────────────────────────────────────────────────────────────

@router.get("/auth/me", response_model=User)
async def me(user: User = Depends(auth.current_user)) -> User:
    return user


def _no_match() -> Exception:
    return human(401, "That name and passphrase don't match", "You're still signed out.",
                 "Check both and try again. Passphrases are case-sensitive.")


@router.post("/auth/login", response_model=User)
def login(body: LoginRequest, request: Request, response: Response) -> User:
    lim = auth.limiters(request)
    name = body.name.strip()
    try:
        auth.check_rate(lim.login, auth.client_ip(request), "login")
        auth.check_rate(lim.login_all, auth.GLOBAL, "login_all")
    except Exception:
        auth.logins.labels("limited").inc()
        raise
    if not auth.valid_name(name):     # no such account can exist; don't let junk names into the backoff table
        auth.logins.labels("bad").inc()
        raise _no_match()
    wait = lim.login_names.wait(name)
    if wait:
        auth.logins.labels("limited").inc()
        err = human(429, "Too many wrong passphrases", "Sign-in for this name is paused for a moment to keep "
                    "the account safe.", f"Wait about {wait} seconds and try again.", [("Retry", "retry")])
        err.headers = {"Retry-After": str(wait)}
        raise err
    try:
        row = db.one("SELECT id, name, role, pass_hash FROM users WHERE name=? AND pass_hash IS NOT NULL", (name,))
    except sqlite3.Error as e:
        raise _db_error(e) from e
    ok = auth.verify_passphrase(body.passphrase, row["pass_hash"] if row else auth.DUMMY_HASH) and row is not None
    if not ok:
        lim.login_names.failed(name)
        auth.logins.labels("bad").inc()
        auth.audit(None, "auth.login_failed", f"user:{name[:40]}")
        raise _no_match()
    assert row is not None
    lim.login_names.succeeded(name)
    with db.tx() as c:
        token, ttl = auth.create_session(c, row["id"], row["role"], user_agent=_ua(request))
    auth.set_session(response, token, ttl)
    user = auth.make_user(row["id"], row["name"], row["role"])
    auth.logins.labels("ok").inc()
    auth.audit(user, "auth.login", f"user:{user.id}", {"client": auth.ua_summary(_ua(request))})
    return user


@router.post("/auth/logout", response_model=OkResponse)
async def logout(request: Request, response: Response) -> OkResponse:
    user = await auth.optional_user(request)
    sess = auth.session_of(request)
    if sess is not None:
        with db.tx() as c:
            auth.end_sessions(c, token_hash_=sess.token_hash)
            if sess.device_id:      # devices can't sign back in; signing out unpairs (module docstring)
                c.execute("UPDATE devices SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                          (time.time(), sess.device_id))
                auth.end_sessions(c, device_id=sess.device_id)
        hub.disconnect(session=sess.token_hash)
        if sess.device_id:
            hub.disconnect(device_id=sess.device_id)
        auth.audit(user, "auth.logout", f"device:{sess.device_id}" if sess.device_id else f"user:{sess.user_id}")
    auth.clear_cookie(response, SESSION_COOKIE)
    return OkResponse(message="Signed out")


# ── first-run setup (§83) ────────────────────────────────────────────────────────────────────

def _has_users() -> bool:
    return db.one("SELECT 1 FROM users LIMIT 1") is not None


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip().replace("\x00", "")
    except OSError:
        return ""


def _hardware() -> DetectedItem:
    name = settings.hardware() or _read("/sys/class/dmi/id/product_name").replace("_", " ")
    if name and any(k in name.upper() for k in ("DGX", "GB10")):
        return DetectedItem(label="DGX Spark", value=name, ok=True)
    return DetectedItem(label="DGX Spark", value=f"Not detected{f' ({name})' if name else ''} — "
                        "Labzilla still runs, but GPU sharing assumes a DGX Spark", ok=False)


def _gpu() -> DetectedItem:
    try:
        from lif.console import poller
        snap = poller.snapshot()
    except Exception:
        return DetectedItem(label="GPU", value="Not readable yet", ok=False)
    if not snap.updated_at:
        return DetectedItem(label="GPU", value="Checking…", ok=False)
    r = snap.status.resource
    if not r.primary_enabled and r.gpu_util_pct is None and r.mem_total_gb is None:
        return DetectedItem(label="GPU", value="No external scheduler configured", ok=True)
    if snap.errors.get("gpu") or (r.gpu_util_pct is None and r.mem_total_gb is None):
        return DetectedItem(label="GPU", value="Not readable yet — the GPU scheduler didn't answer", ok=False)
    parts = [f"{r.mem_total_gb:.0f} GB unified memory" if r.mem_total_gb else "",
             f"{r.gpu_util_pct:.0f}% busy" if r.gpu_util_pct is not None else "",
             r.blerbz_label if r.primary_enabled else ""]
    return DetectedItem(label="GPU", value=" · ".join(p for p in parts if p), ok=True)


def _detected(request: Request) -> list[DetectedItem]:
    in_cluster = bool(os.environ.get("KUBERNETES_SERVICE_HOST"))
    secure = auth.is_secure(request)
    return [
        _hardware(),
        DetectedItem(label="K3s", value="Running in the cluster" if in_cluster
                     else "Not in a cluster (development mode)", ok=in_cluster),
        _gpu(),
        DetectedItem(label="Network", value=f"{settings.public_url()}"
                     + ("" if secure else " — this connection isn't HTTPS"), ok=secure),
    ]


def _setup_norm(code: str) -> str:
    """Typed by a person from a terminal ('abcd efgh-…'): case, spaces and dashes don't matter. The generated
    code (create-secrets.sh) is upper-case and fixed-length, so this costs no entropy."""
    return re.sub(r"[\s-]+", "", code).upper()


@router.get("/setup", response_model=SetupState)
async def setup_state(request: Request) -> SetupState:
    try:
        needs = not _has_users()
    except sqlite3.Error as e:
        raise _db_error(e) from e
    # Hardware and addresses are for the person setting up (or signed in), not for any LAN visitor.
    show = needs or await auth.optional_user(request) is not None
    return SetupState(needs_setup=needs, setup_code_configured=settings.setup_code() is not None,
                      detected=_detected(request) if show else [], public_url=settings.public_url(),
                      alt_urls=settings.alt_urls(), lan_url=settings.lan_url() if show else None)


@router.post("/setup", response_model=User)
def setup(body: SetupRequest, request: Request, response: Response) -> User:
    auth.check_rate(auth.limiters(request).setup, auth.client_ip(request), "setup")
    auth.check_rate(auth.limiters(request).setup_all, auth.GLOBAL, "setup_all")
    code = settings.setup_code()
    if code is None:
        raise human(503, "Setup code isn't configured", "No admin account can be created yet.",
                    "Run lif/scripts/create-secrets.sh on the host, then reload this page.", [("Reload", "reload")])
    if _has_users():
        raise human(409, "Labzilla is already set up", "No new admin account was created.",
                    "Sign in with the existing account.", [("Sign in", "login")])
    if not hmac.compare_digest(_setup_norm(body.setup_code).encode(), _setup_norm(code).encode()):
        auth.logins.labels("setup_bad").inc()
        auth.audit(None, "setup.bad_code", "setup")
        raise human(403, "That setup code isn't right", "No account was created.",
                    "Read the code from secrets/lif-console-setup.code on the Labzilla host and try again.")
    name = body.name.strip()
    if not auth.valid_name(name):
        raise human(422, "Choose a different name", "No account was created.",
                    "Use 1–40 letters, numbers, spaces, dots, @ or dashes.")
    if not auth.valid_passphrase(body.passphrase):
        raise human(422, "Choose a longer passphrase", "No account was created.",
                    f"Use at least {auth.MIN_PASSPHRASE} characters — a few words is easiest.")
    pass_hash = auth.hash_passphrase(body.passphrase)
    now = time.time()
    uid = auth.new_id("u")
    with db.tx() as c:
        if c.execute("SELECT 1 FROM users LIMIT 1").fetchone():     # lost a race with another setup
            raise human(409, "Labzilla is already set up", "No new admin account was created.",
                        "Sign in with the existing account.", [("Sign in", "login")])
        c.execute("INSERT INTO users(id, name, role, pass_hash, created_at) VALUES(?,?,?,?,?)",
                  (uid, name, "admin", pass_hash, now))
        c.execute("INSERT INTO kv(key, value, updated_at) VALUES('default_mode',?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                  (body.default_mode, now))
        token, ttl = auth.create_session(c, uid, "admin", user_agent=_ua(request), now=now)
    auth.set_session(response, token, ttl)
    user = auth.make_user(uid, name, "admin")
    auth.logins.labels("setup").inc()
    auth.audit(user, "setup.admin_created", f"user:{uid}", {"default_mode": body.default_mode})
    return user


# ── access (§14, §63, §78) ───────────────────────────────────────────────────────────────────

@router.get("/access", response_model=AccessInfo)
async def access(request: Request) -> AccessInfo:
    """How to reach and trust this console. Open (the Trust page helps before sign-in); it says only
    what the LAN can already see."""
    secure = auth.is_secure(request)
    hint = ("Connected over HTTPS. If your browser warned about the certificate, install Labzilla's "
            "certificate so install, voice and notifications work — see Trust this device." if secure else
            "This connection isn't encrypted. Signing in still works on your network, but install, voice "
            "and notifications need trusted HTTPS — see Trust this device.")
    ca = local_ca()
    return AccessInfo(public_url=settings.public_url(), alt_urls=settings.alt_urls(), lan_url=settings.lan_url(),
                      secure=secure, trusted_hint=hint, mdns=settings.mdns(),  # type: ignore[arg-type]
                      ca_available=ca is not None, ca_sha256=ca[1] if ca else None)


# ── the Labzilla Local CA certificate (public: every HTTPS handshake already sends it) ───────────────

CA_FILE = Path(os.environ.get("LIF_CONSOLE_CA_FILE", "/etc/labzilla-ca/ca.crt"))
_PEM = re.compile(r"-----BEGIN CERTIFICATE-----([A-Za-z0-9+/=\s]+)-----END CERTIFICATE-----")


def local_ca() -> tuple[str, str] | None:
    """(PEM, SHA-256 fingerprint) of the CA certificate the console was given, or None. Only a certificate
    is ever read or served: anything else in the file (a key) is refused."""
    import base64
    try:
        text = CA_FILE.read_text()
    except OSError:
        return None
    m = _PEM.search(text)
    if m is None or "PRIVATE KEY" in text:
        return None
    try:
        der = base64.b64decode("".join(m.group(1).split()), validate=True)
    except ValueError:                  # binascii.Error: a damaged file is "no CA", not a 500
        return None
    fp = hashlib.sha256(der).hexdigest().upper()
    return m.group(0) + "\n", ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))


@router.get("/trust/ca.crt", include_in_schema=False)
async def ca_certificate() -> Response:
    """Download the Labzilla Local CA certificate (open: a new device needs it before it can trust the
    console). Check its fingerprint against Trust this device on a device that already trusts it."""
    ca = local_ca()
    if ca is None:
        return PlainTextResponse("This console has no CA certificate to offer.", status_code=404)
    return Response(ca[0], media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="labzilla-ca.crt"',
                             "X-Labzilla-CA-SHA256": ca[1], "Cache-Control": "no-cache"})


# ── pairing (§15–§17) ────────────────────────────────────────────────────────────────────────

def _code(token: str, claim_secret: str) -> str:
    mac = hmac.new(token.encode(), b"labzilla-pair-code:" + claim_secret.encode(), hashlib.sha256).digest()
    return f"{int.from_bytes(mac[:8], 'big') % 1_000_000:06d}"


_CTRL = re.compile(r"[\x00-\x1f\x7f]+")


def _device_name(raw: str, ua: str) -> str:
    name = " ".join(_CTRL.sub(" ", raw).split())[:60]
    return name or auth.ua_summary(ua)


def _pairing(row: sqlite3.Row, now: float | None = None) -> Pairing:
    status: PairStatus = row["status"]
    if status in ("waiting", "claimed") and row["expires_at"] < (time.time() if now is None else now):
        status = "expired"
    return Pairing(id=row["id"], expires_at=row["expires_at"], status=status, code=row["code"],
                   device_name=row["device_name"])


def _approval(row: sqlite3.Row) -> Approval:
    p = _pairing(row)
    who = auth.ua_summary(row["user_agent"])
    state = {"claimed": "pending", "approved": "approved", "rejected": "rejected"}.get(p.status, "expired")
    return Approval(
        id=f"pairing:{p.id}", kind="device_pairing", title=f"Pair “{p.device_name}”?",
        action="Let this device use Labzilla",
        why=f"A device ({who}) opened your pairing link. Check that it shows the code {p.code}.",
        impact="It will be able to ask, see status, pause jobs and answer approvals — not change models, "
               "settings or devices. You can revoke it any time.",
        options=[ApprovalOption(value="approve", label="Approve", tone="primary",
                                description=f"Pair {p.device_name}"),
                 ApprovalOption(value="reject", label="Reject", tone="danger",
                                description="The device stays signed out")],
        created_at=row["claimed_at"] or row["created_at"], status=state, blocking=True,  # type: ignore[arg-type]
        tech=[TechDetail(label="Pairing", value=p.id), TechDetail(label="Browser", value=who),
              TechDetail(label="Expires", value=time.strftime("%H:%M:%S UTC", time.gmtime(p.expires_at)))])


def _announce(row: sqlite3.Row) -> None:
    hub.publish("pairing", _pairing(row), audience="admin")
    hub.publish("approval", _approval(row), audience="admin")


def _expire(c: sqlite3.Connection, now: float) -> list[sqlite3.Row]:
    """Mark overdue pairings expired (lazy: on every pairing read/write); returns the claimed ones
    that just expired so admins' approval cards update."""
    rows = c.execute("SELECT * FROM pairings WHERE status IN ('waiting','claimed') AND expires_at < ?",
                     (now,)).fetchall()
    if rows:
        c.execute("UPDATE pairings SET status='expired' WHERE status IN ('waiting','claimed') AND expires_at < ?",
                  (now,))
        auth.pairings.labels("expired").inc(len(rows))
    return [r for r in rows if r["status"] == "claimed"]


def _sweep(now: float) -> None:
    with db.tx() as c:
        gone = [r["id"] for r in _expire(c, now)]
        rows = [c.execute("SELECT * FROM pairings WHERE id=?", (i,)).fetchone() for i in gone]
    for r in rows:
        _announce(r)


def pending_approvals() -> list[Approval]:
    """Claimed pairings waiting for an admin, as Approval cards (for GET /api/approvals)."""
    _sweep(time.time())
    return [_approval(r) for r in db.q("SELECT * FROM pairings WHERE status='claimed' ORDER BY claimed_at")]


def decide(pairing_id: str, approve: bool, user: User) -> Pairing:
    """Approve or reject a claimed pairing (caller has checked devices.manage). Raises human errors."""
    if "devices.manage" not in user.perms:
        raise human(403, "Not allowed from this session", "Only an admin can pair devices. Nothing was changed.",
                    "Approve it from an admin session on a desktop.", tech={"permission": "devices.manage"})
    now = time.time()
    with db.tx() as c:
        _expire(c, now)
        row = c.execute("SELECT * FROM pairings WHERE id=?", (pairing_id,)).fetchone()
        if row is None:
            raise human(404, "Pairing not found", "Nothing was changed.", "Start a new pairing from Connect Mobile.",
                        [("Connect Mobile", "/connect")])
        if row["status"] != "claimed":
            what = {"waiting": "No device has opened this pairing link yet",
                    "approved": "This device is already paired", "rejected": "This pairing was rejected",
                    "expired": "This pairing expired"}[row["status"]]
            raise human(409, what, "Nothing was changed.", "Start a new pairing from Connect Mobile if needed.",
                        [("Connect Mobile", "/connect")])
        if approve:
            device_id = auth.new_id("d")
            c.execute("INSERT INTO devices(id, user_id, name, user_agent, paired_at) VALUES(?,?,?,?,?)",
                      (device_id, row["created_by"], row["device_name"], row["user_agent"], now))
            c.execute("UPDATE pairings SET status='approved', decided_by=?, decided_at=?, device_id=?, expires_at=? "
                      "WHERE id=? AND status='claimed'",
                      (user.id, now, device_id, now + settings.pairing_ttl_sec(), pairing_id))
        else:
            c.execute("UPDATE pairings SET status='rejected', decided_by=?, decided_at=? "
                      "WHERE id=? AND status='claimed'", (user.id, now, pairing_id))
        row = c.execute("SELECT * FROM pairings WHERE id=?", (pairing_id,)).fetchone()
    step = "approved" if approve else "rejected"
    auth.pairings.labels(step).inc()
    auth.audit(user, f"pairing.{step}", f"pairing:{pairing_id}",
               {"device": row["device_name"], "device_id": row["device_id"]})
    _announce(row)
    return _pairing(row)


@router.post("/pair/start", response_model=Pairing)
def pair_start(request: Request, user: User = Depends(auth.require("devices.manage"))) -> Pairing:
    auth.check_rate(auth.limiters(request).pair_start, user.id, "pair_start")
    now = time.time()
    token, pid = auth.new_token(), auth.new_id("p")
    ttl = settings.pairing_ttl_sec()
    with db.tx() as c:
        _expire(c, now)
        c.execute("INSERT INTO pairings(id, token_hash, created_by, created_at, expires_at, status) "
                  "VALUES(?,?,?,?,?, 'waiting')", (pid, auth.token_hash(token), user.id, now, now + ttl))
    auth.pairings.labels("started").inc()
    auth.audit(user, "pairing.started", f"pairing:{pid}")
    # The token rides in the fragment: browsers never send it, so it can't land in an access log.
    return Pairing(id=pid, url=f"{settings.public_url()}/pair#{token}", token=token, expires_at=now + ttl,
                   status="waiting")


def _claimed_by(request: Request) -> sqlite3.Row | None:
    cookie = request.cookies.get(PAIR_COOKIE)
    if not cookie or len(cookie) > 200:
        return None
    return db.one("SELECT * FROM pairings WHERE claim_hash=?", (auth.token_hash(cookie),))


@router.post("/pair/claim", response_model=Pairing)
def pair_claim(body: PairClaimRequest, request: Request, response: Response) -> Pairing:
    auth.check_rate(auth.limiters(request).pair_claim, auth.client_ip(request), "pair_claim")
    auth.check_rate(auth.limiters(request).pair_claim_all, auth.GLOBAL, "pair_claim_all")
    now = time.time()
    th = auth.token_hash(body.token.strip())
    mine = _claimed_by(request)
    if mine is not None and mine["token_hash"] == th:       # this phone already claimed it (double tap)
        return _pairing(mine, now)
    claim = secrets.token_urlsafe(32)
    ua = _ua(request)
    name = _device_name(body.device_name, ua)
    ttl = settings.pairing_ttl_sec()
    with db.tx() as c:
        _expire(c, now)
        row = c.execute("SELECT * FROM pairings WHERE token_hash=?", (th,)).fetchone()
        claimed = row is not None and c.execute(
            "UPDATE pairings SET status='claimed', claim_hash=?, code=?, device_name=?, user_agent=?, claimed_at=?,"
            " expires_at=? WHERE id=? AND status='waiting'",
            (auth.token_hash(claim), _code(body.token.strip(), claim), name, ua, now, now + ttl,
             row["id"])).rowcount == 1
        if claimed:
            assert row is not None
            row = c.execute("SELECT * FROM pairings WHERE id=?", (row["id"],)).fetchone()
    if not claimed:
        auth.pairings.labels("claim_refused").inc()
        expired = row is not None and row["status"] == "expired"
        raise human(410 if row is not None else 404,
                    "This pairing link expired" if expired else "This pairing link can't be used",
                    "This device isn't paired.",
                    "On your desktop, open Connect Mobile and scan a fresh code." if expired else
                    "Each code works once and only for a short time. Scan a fresh code from Connect Mobile.")
    assert row is not None
    auth.set_cookie(response, PAIR_COOKIE, claim, ttl * 3, path="/api/pair")
    auth.pairings.labels("claimed").inc()
    auth.audit(None, "pairing.claimed", f"pairing:{row['id']}", {"device": name, "client": auth.ua_summary(ua)})
    _announce(row)
    return _pairing(row, now)


# Declared before /pair/{pairing_id} so "status" is never taken for an id.
@router.get("/pair/status", response_model=Pairing)
def pair_status(request: Request, response: Response) -> Pairing:
    """The phone's poll. On approval it receives its device session here, exactly once."""
    # The claim cookie comes first and keys the limit: a caller without one learns nothing and can't
    # spend the waiting phone's budget (behind SNAT every LAN client shares one address).
    row = _claimed_by(request)
    if row is None:
        raise human(404, "No pairing in progress on this device", "This device isn't paired.",
                    "Scan the code shown in Connect Mobile on your desktop.")
    auth.check_rate(auth.limiters(request).pair_status, row["claim_hash"], "pair_status")
    now = time.time()
    _sweep(now)
    row = db.one("SELECT * FROM pairings WHERE id=?", (row["id"],)) or row     # the sweep may have expired it
    if row["status"] == "approved" and row["expires_at"] < now and row["delivered_at"] is None:
        with db.tx() as c:      # approved but never collected: don't leave a half-paired device behind
            c.execute("UPDATE pairings SET status='expired' WHERE id=? AND delivered_at IS NULL", (row["id"],))
            c.execute("UPDATE devices SET revoked_at=? WHERE id=?", (now, row["device_id"]))
            row = c.execute("SELECT * FROM pairings WHERE id=?", (row["id"],)).fetchone()
    if row["status"] == "approved":
        with db.tx() as c:
            first = c.execute("UPDATE pairings SET delivered_at=? WHERE id=? AND status='approved' "
                              "AND delivered_at IS NULL", (now, row["id"])).rowcount == 1
            if first:
                owner = c.execute("SELECT id, name FROM users WHERE id=?", (row["created_by"],)).fetchone()
                token, ttl = auth.create_session(c, row["created_by"], "device", device_id=row["device_id"],
                                                 user_agent=_ua(request), now=now)
        if first:
            auth.set_session(response, token, ttl)
            auth.clear_cookie(response, PAIR_COOKIE, path="/api/pair")
            auth.pairings.labels("delivered").inc()
            auth.audit(auth.make_user(owner["id"], owner["name"], "device", row["device_name"]),
                       "pairing.session_issued", f"device:{row['device_id']}")
    elif row["status"] in ("rejected", "expired"):
        auth.clear_cookie(response, PAIR_COOKIE, path="/api/pair")
    return _pairing(row, now)


@router.get("/pair/{pairing_id}", response_model=Pairing)
def pair_get(pairing_id: str, user: User = Depends(auth.require("devices.manage"))) -> Pairing:
    _sweep(time.time())
    row = db.one("SELECT * FROM pairings WHERE id=?", (pairing_id,))
    if row is None:
        raise human(404, "Pairing not found", "", "Start a new pairing from Connect Mobile.",
                    [("Connect Mobile", "/connect")])
    return _pairing(row)


@router.post("/pair/{pairing_id}/approve", response_model=Pairing)
def pair_approve(pairing_id: str, user: User = Depends(auth.require("devices.manage"))) -> Pairing:
    return decide(pairing_id, True, user)


@router.post("/pair/{pairing_id}/reject", response_model=Pairing)
def pair_reject(pairing_id: str, user: User = Depends(auth.require("devices.manage"))) -> Pairing:
    return decide(pairing_id, False, user)


# ── devices ──────────────────────────────────────────────────────────────────────────────────

@router.get("/devices", response_model=list[Device])
def devices(request: Request, user: User = Depends(auth.require("devices.manage"))) -> list[Device]:
    sess = auth.session_of(request)
    rows = db.q("SELECT * FROM devices WHERE revoked_at IS NULL ORDER BY paired_at DESC")
    return [Device(id=r["id"], name=r["name"], paired_at=r["paired_at"], last_seen=r["last_seen"],
                   user_agent_summary=auth.ua_summary(r["user_agent"]),
                   current=bool(sess and sess.device_id == r["id"])) for r in rows]


@router.delete("/devices/{device_id}", response_model=OkResponse)
def revoke_device(device_id: str, user: User = Depends(auth.require("devices.manage"))) -> OkResponse:
    with db.tx() as c:
        row = c.execute("SELECT name FROM devices WHERE id=? AND revoked_at IS NULL", (device_id,)).fetchone()
        if row is None:
            raise human(404, "Device not found", "It may already have been removed.", "Refresh the device list.")
        c.execute("UPDATE devices SET revoked_at=? WHERE id=?", (time.time(), device_id))
        ended = auth.end_sessions(c, device_id=device_id)
    hub.disconnect(device_id=device_id)
    auth.audit(user, "device.revoked", f"device:{device_id}", {"device": row["name"], "sessions_ended": ended})
    return OkResponse(message=f"{row['name']} was signed out and unpaired")
