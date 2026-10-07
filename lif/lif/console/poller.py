"""Background poller: reads upstreams once per poll_sec and serves every viewer from one cache (spec §60, §61).

Builds SystemStatus / ComputeView / ModelRole / ModelDeployment / ActivityEvent snapshots, diffs them
against the previous cycle, and publishes hub events (status, activity, jobs, model, approval,
notification). Notifications are only the §39/§100 kinds. Routes and the command bar read
`snapshot()` and never block on an upstream (GET /api/system/status answers in < 200 ms).

Each upstream source has its own cadence and its own last-good cache, and runs as its own task:
controller /v1/overview fans out sequentially (5 s timeouts each), so one slow service must not make
Home stale. A cycle starts the sources that are due, waits a bounded time, and builds the snapshot
from whatever each source last returned — a source that is still in flight lands in the next cycle.

Owner: SYS.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

from lif.common import log
from lif.console import humanize as hz
from lif.console import earn, settings, upstream
from lif.console.contracts import (ActivityEvent, Approval, ApprovalOption, BenchmarkSummary, CanaryInfo,
                                   ComputeView, ConnectionInfo, Health, JobsEvent, JobsSummary, ModelAction,
                                   ModelDeployment, ModelEvent, ModelRole, Notification, NotificationKind,
                                   ResourceState, ServiceHealth, Severity, SystemStatus, TechDetail, WorkShare)
from lif.console.events import hub
from lif.console.upstream import UpstreamError

LOG = log.get("lif.console.poller")

GIB = 2 ** 30
CORE_SERVICES = ("gateway", "controller", "decision", "batch")    # an outage of these is notified (§39)
SEED_AFTER_SEC = 60.0


@dataclass
class Snapshot:
    """Everything the console knows right now. `raw` keeps the last upstream payloads by source
    (e.g. "gpu", "overview", "capabilities", "routing", "batch_stats", "settings") for intent answers
    and Technical Details; it never goes to the browser as-is."""
    status: SystemStatus = field(default_factory=lambda: SystemStatus(headline="Checking Labzilla…"))
    compute: ComputeView = field(default_factory=ComputeView)
    roles: list[ModelRole] = field(default_factory=list)
    deployments: list[ModelDeployment] = field(default_factory=list)
    activity: list[ActivityEvent] = field(default_factory=list)       # newest first
    services: list[ServiceHealth] = field(default_factory=list)
    approvals: list[Approval] = field(default_factory=list)           # pending only
    jobs: JobsSummary = field(default_factory=JobsSummary)
    raw: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)              # source → why it couldn't be read
    updated_at: float = 0.0


# ── sources ───────────────────────────────────────────────────────────────────────────────────

@dataclass
class _Src:
    value: Any = None                 # last good payload (kept while later attempts fail)
    ok_ts: float = 0.0                # when value was fetched
    attempt_ts: float = 0.0
    error: str | None = None          # human reason the LAST attempt failed (None = it succeeded)
    exc: UpstreamError | None = None
    task: asyncio.Task[None] | None = None

    def ok(self) -> bool:
        return self.ok_ts > 0 and self.error is None


PROM_QUERIES: dict[str, str] = {
    "mem_total": "max(node_memory_MemTotal_bytes)",
    "mem_avail": "max(node_memory_MemAvailable_bytes)",
    "residents_mib": "max(gpusched_capacity_residents_mib)",
    "leases_mib": "max(gpusched_capacity_leases_mib)",
    "lif_mem": 'sum(container_memory_working_set_bytes{namespace=~"ai-serving|ai-system|ai-batch",'
               'container!="",container!="POD"})',
    "temp": "max(node_thermal_zone_temp) or max(node_hwmon_temp_celsius)",
    "alerts": 'ALERTS{alertstate="firing"}',
    "waiting": 'kube_pod_container_status_waiting_reason{namespace=~"ai-system|ai-serving|ai-batch|earn"} == 1',
    "restarts": 'sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total'
                '{namespace=~"ai-system|ai-serving|ai-batch|earn"}[15m])) > 0',
    "terminated": 'kube_pod_container_status_last_terminated_reason{namespace=~"ai-system|ai-serving|ai-batch|earn"} == 1',
    "deploy_avail": 'kube_deployment_status_replicas_available{namespace=~"ai-system|ai-serving|ai-batch|earn"}',
    "deploy_spec": 'kube_deployment_spec_replicas{namespace=~"ai-system|ai-serving|ai-batch|earn"}',
    "earn_backup_ok": 'kube_cronjob_status_last_successful_time{namespace="earn",cronjob="earn-backup"}',
}


async def _prom_all() -> dict[str, list[dict[str, Any]]]:
    # The first query is strict so an unreachable Prometheus is reported as an error, not as "no data".
    first, *rest = PROM_QUERIES
    out = {first: await upstream.prom(PROM_QUERIES[first], strict=True)}
    vals = await asyncio.gather(*(upstream.prom(PROM_QUERIES[k]) for k in rest))
    out.update(zip(rest, vals))
    return out


async def _earn_status() -> Any:
    """earn's /api/status (read key). None when no earn URL is configured: System then shows Earn from kube-state."""
    if not settings.earn_url():
        return None
    return await upstream.get("earn", "/api/status", timeout=5.0)


ACTIVITY_PAGE, ACTIVITY_MAX = 200, 2000      # the controller returns the NEWEST `limit` rows after `since`


async def _activity() -> list[dict[str, Any]]:
    async def fetch(limit: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if _st.max_seq:
            params["since"] = _st.max_seq
        body = await upstream.get("controller", "/v1/activity", params=params)
        rows = body.get("activity") if isinstance(body, dict) else None
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    rows = await fetch(ACTIVITY_PAGE)
    if _st.primed and len(rows) >= ACTIVITY_PAGE and _min_seq(rows) > _st.max_seq + 1:
        # A burst bigger than one page since the last poll: the oldest new rows were cut off. Ask for
        # the controller's maximum instead of skipping them for good.
        rows = await fetch(ACTIVITY_MAX)
        if len(rows) >= ACTIVITY_MAX and _min_seq(rows) > _st.max_seq + 1:
            log.event(LOG, "activity_gap", since=_st.max_seq, oldest=_min_seq(rows))
    return rows


def _min_seq(rows: list[dict[str, Any]]) -> int:
    seqs = [int(n) for n in (hz.num(r.get("seq")) for r in rows) if n is not None]
    return min(seqs) if seqs else 0


# name → (cadence seconds, fetch). The fast ones drive Home; the slow ones are heavier or rate-limited
# (gateway /v1/capabilities counts against the console's gateway quota; /v1/health does not).
SOURCES: dict[str, tuple[float, Callable[[], Awaitable[Any]]]] = {
    "health": (0, lambda: upstream.get("gateway", "/v1/health", timeout=3.0)),
    "gpu": (0, lambda: upstream.get("controller", "/v1/gpu", timeout=3.0)),
    "overview": (0, lambda: upstream.get("controller", "/v1/overview", timeout=20.0)),
    "activity": (0, _activity),
    "prom": (15, _prom_all),
    "routing": (30, lambda: upstream.get("controller", "/v1/routing")),
    "capabilities": (30, lambda: upstream.get("gateway", "/v1/capabilities")),
    "human": (30, lambda: upstream.get("controller", "/v1/de/human", params={"status": "pending"})),
    "models": (30, lambda: upstream.get("controller", "/v1/models", timeout=8.0)),
    "knowledge": (60, lambda: upstream.get("knowledge", "/healthz", timeout=3.0)),
    # status() is computed inside earn's trading event loop on every call: keep this slow
    "earn": (30, _earn_status),
}


# ── notifications (§39, §100): conditions while they hold + discrete events ─────────────────

class _Notifier:
    """Condition notifications (fallback, memory, service down…) exist while the condition holds and are
    published once when raised; event notifications (candidate, job done) are kept for a day. Nothing is
    published during the first cycle, so a console restart doesn't re-announce everything."""

    def __init__(self) -> None:
        self.active: dict[str, Notification] = {}
        self.events: deque[Notification] = deque(maxlen=30)
        self.seeded = False
        self.outbox: list[Notification] = []

    def condition(self, key: str, on: bool, kind: NotificationKind, severity: Severity, title: str, body: str,
                  href: str | None = None, now: float = 0.0) -> None:
        if not on:
            self.active.pop(key, None)
            return
        cur = self.active.get(key)
        if cur is None:
            n = Notification(id=f"{key}:{int(now)}", ts=now, severity=severity, title=title, body=body, href=href,
                             kind=kind)
            self.active[key] = n
            if self.seeded:
                self.outbox.append(n)
        else:       # keep id/ts (de-dup), refresh the wording (counts change)
            self.active[key] = cur.model_copy(update={"title": title, "body": body, "severity": severity})

    def event(self, key: str, kind: NotificationKind, severity: Severity, title: str, body: str,
              href: str | None, now: float) -> None:
        if any(n.id == key for n in self.events):
            return
        n = Notification(id=key, ts=now, severity=severity, title=title, body=body, href=href, kind=kind)
        self.events.appendleft(n)
        if self.seeded:
            self.outbox.append(n)

    def current(self, now: float) -> list[Notification]:
        recent = [n for n in self.events if now - n.ts < 86400]
        return sorted([*self.active.values(), *recent], key=lambda n: n.ts, reverse=True)[:20]


@dataclass
class _State:
    src: dict[str, _Src] = field(default_factory=lambda: {k: _Src() for k in SOURCES})
    rows: dict[int, dict[str, Any]] = field(default_factory=dict)     # activity rows by seq
    max_seq: int = 0
    primed: bool = False                                              # activity fetched successfully once
    echoes: dict[str, float] = field(default_factory=dict)            # setting re-sent unchanged → when
    new_rows: list[dict[str, Any]] = field(default_factory=list)      # arrived since the last build
    notifier: _Notifier = field(default_factory=_Notifier)
    jobs_seen: dict[str, str] = field(default_factory=dict)           # batch job id → state
    started_at: float = field(default_factory=time.time)
    cycles: int = 0
    loop_task: asyncio.Task[None] | None = None
    keys: dict[str, Any] = field(default_factory=dict)                # last published fingerprints


_st = _State()
_current = Snapshot()
_background: set[asyncio.Task[Any]] = set()
_lock: asyncio.Lock | None = None


def snapshot() -> Snapshot:
    """The latest snapshot (never blocks; before the first cycle: health unknown, 'Checking Labzilla…')."""
    return _current


def reset() -> None:
    """Forget everything (tests)."""
    global _st, _current, _lock
    _st, _current, _lock = _State(), Snapshot(), None


def running() -> bool:
    return _st.loop_task is not None and not _st.loop_task.done()


async def _run(name: str) -> None:
    s = _st.src[name]
    s.attempt_ts = time.time()
    try:
        value = await SOURCES[name][1]()
    except UpstreamError as e:
        s.error, s.exc = upstream.reason(e), e
        return
    except Exception as e:      # an unexpected shape must never stop the poller
        log.event(LOG, "source_failed", source=name, error=type(e).__name__)
        s.error, s.exc = "Unexpected response", None
        return
    if name == "activity":
        _merge_activity(value)
        value = None
    s.value, s.ok_ts, s.error, s.exc = value, time.time(), None, None


def note_echo(key: str) -> None:
    """A setting is about to be re-posted unchanged (set_setting's pause workaround): the controller
    logs every posted key, so its "turned on" row would announce a change that didn't happen."""
    _st.echoes[key] = time.time()


def _is_echo(r: dict[str, Any]) -> bool:
    if r.get("kind") != "setting_changed":
        return False
    key = str(r.get("subject") or "")
    at = _st.echoes.get(key)
    if at is None or _d(r.get("detail")).get("value") is not True or abs((hz.num(r.get("ts")) or 0) - at) > 120:
        return False
    del _st.echoes[key]
    return True


def _merge_activity(rows: list[dict[str, Any]]) -> None:
    first = not _st.primed          # the backlog at startup is history, not news; an empty first fetch still primes
    _st.primed = True
    for r in rows:
        try:
            seq = int(r.get("seq") or 0)
        except (TypeError, ValueError):
            continue
        if seq in _st.rows:
            continue
        if _is_echo(r):
            _st.max_seq = max(_st.max_seq, seq)       # consumed: the next poll must not fetch it again
            continue
        _st.rows[seq] = r
        if not first:
            _st.new_rows.append(r)
        _st.max_seq = max(_st.max_seq, seq)
    for seq in sorted(_st.rows)[:-300]:       # keep the newest 300
        del _st.rows[seq]


async def _cycle(force: bool, wait: float) -> Snapshot:
    global _current, _lock
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        now = time.time()
        pending: list[asyncio.Task[None]] = []
        for name, (every, _) in SOURCES.items():
            s = _st.src[name]
            if s.task is not None and not s.task.done():
                pending.append(s.task)
                continue
            if name == "capabilities" and not settings.gateway_key():
                continue                    # overview carries the controller's view of capabilities
            if name == "knowledge" and not settings.knowledge_url():
                continue
            if force or now - s.attempt_ts >= every:
                s.task = asyncio.create_task(_run(name))
                pending.append(s.task)
        if pending:
            await asyncio.wait(pending, timeout=wait)
        _st.cycles += 1
        try:
            snap = build(time.time())
        except Exception as e:      # a shape nobody anticipated: keep serving the last good snapshot
            log.event(LOG, "build_failed", error=type(e).__name__, detail=str(e)[:200])
            _st.new_rows = []
            return _current
        _publish(_current, snap)
        _current = snap
        # Announce only after the core sources have each answered once (or a minute has passed): otherwise a
        # slow /v1/overview arriving in cycle 2 would re-announce conditions that were already true at restart.
        if not _st.notifier.seeded and (all(_st.src[n].ok_ts for n in ("health", "gpu", "overview"))
                                        or time.time() - _st.started_at > SEED_AFTER_SEC):
            _st.notifier.seeded = True
        return snap


async def refresh() -> Snapshot:
    """Run one poll cycle now (used after a mutation so the next read reflects it)."""
    return await _cycle(force=True, wait=8.0)


def refresh_soon() -> None:
    """Schedule refresh() without awaiting it (keeps a reference so the task isn't collected mid-run)."""
    try:
        t = asyncio.get_running_loop().create_task(refresh())
    except RuntimeError:
        return
    _background.add(t)
    t.add_done_callback(_background.discard)


def ensure_fresh() -> None:
    """Called by read routes: if no loop is running and the snapshot is old, refresh in the background."""
    if not running() and time.time() - _current.updated_at > 2 * settings.poll_sec() and not _background:
        refresh_soon()


async def _loop() -> None:
    while True:
        t0 = time.monotonic()
        poll = max(1.0, settings.poll_sec())
        try:
            await _cycle(force=False, wait=min(4.0, poll * 0.8))
        except asyncio.CancelledError:
            raise
        except Exception as e:      # keep serving the last snapshot; never die
            log.event(LOG, "cycle_failed", error=type(e).__name__, detail=str(e)[:200])
        await asyncio.sleep(max(0.5, poll - (time.monotonic() - t0)))


async def start(app: Any = None) -> None:
    """Start the background loop (called from the app lifespan)."""
    if not running():
        _st.loop_task = asyncio.create_task(_loop())


async def stop(app: Any = None) -> None:
    tasks = [t for t in (_st.loop_task, *(s.task for s in _st.src.values()), *_background) if t and not t.done()]
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    _st.loop_task = None


# ── builders (pure; also used by routes/models.py) ───────────────────────────────────────────

def _d(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) and "error" not in v else {}


def deployment_name(profile: dict[str, Any] | None) -> str | None:
    """ai-serving Deployment behind a routing profile = its endpoint host's first label (lifecycle.deployment_name)."""
    ep = (profile or {}).get("endpoint") if isinstance(profile, dict) else None
    host = urlparse(str(ep)).hostname if ep else None
    return host.split(".")[0] if host else None


def shed_profiles(routing: dict[str, Any], shed: list[str]) -> set[str]:
    """Profiles whose server the memory guard has scaled to zero. Matched by endpoint host only, so the guard's
    'embedding' Deployment is never confused with BLERBZ's own GPU embedder (a gpusched resident)."""
    profiles = _d(routing.get("profiles"))
    return {p for p, prof in profiles.items() if deployment_name(prof) in set(shed)}


def physical(pid: str | None, routing: dict[str, Any], caps: dict[str, Any]) -> str | None:
    if not pid:
        return None
    cp = _d(_d(caps.get("profiles")).get(pid))
    if cp.get("model"):
        return str(cp["model"])
    rp = _d(_d(routing.get("profiles")).get(pid))
    return f"{rp['hf_repo']}@{rp.get('revision') or 'main'}" if rp.get("hf_repo") else None


def build_roles(routing: dict[str, Any], caps: dict[str, Any], shed: list[str], checked: bool = True) -> list[ModelRole]:
    """ModelRole = controller routing (chain, canary, floor) + gateway capabilities (who serves now) + guard."""
    r_aliases = _d(routing.get("aliases"))
    canaries = _d(routing.get("canaries"))
    floors = _d(routing.get("alias_min_params_b"))
    c_aliases = _d(caps.get("aliases"))
    have_caps = bool(c_aliases)
    shed_p = shed_profiles(routing, shed)
    out: list[ModelRole] = []
    for role, alias, label, blurb, _adv in hz.ROLES:
        chain = [str(p) for p in (r_aliases.get(alias) or []) if p] if isinstance(r_aliases.get(alias), list) else []
        st = c_aliases.get(alias) if isinstance(c_aliases.get(alias), dict) else None
        can = _d(canaries.get(alias))
        canary = CanaryInfo(model=hz.model_name(str(can.get("profile"))), percent=hz.num(can.get("percent")) or 0.0) \
            if can.get("profile") else None
        primary = chain[0] if chain else None
        served = str(st.get("served_by")) if st and st.get("served_by") else None
        # alias_status samples canaries at random; pin the display to the routing table so a canary pick
        # isn't reported as a change (or as a fallback) every other cycle.
        canary_pick = bool(can.get("profile")) and served == can.get("profile")
        if canary_pick:
            served = primary or served
        fallback = bool(st and st.get("fallback")) and not canary_pick
        degraded = bool(st and st.get("degraded"))
        available = st.get("available") if st else None
        reason = str(st.get("reason") or "") if st else ""
        shed_hit = primary in shed_p and (fallback or available is False)
        cause, cause_label = hz.role_cause(None if canary_pick else reason, fallback=fallback, degraded=degraded,
                                           shed=shed_hit)
        if cause is None and canary:
            cause, cause_label = "canary", f"Trialling {canary.model} on {canary.percent:g}% of requests"
        if alias == "local/auto":         # a router, not a model: it is as available as the gateway says
            auto: tuple[Health, str] = ("unknown", "Unknown" if checked else "Checking") if st is None else \
                ("healthy", "Ready") if available else ("offline", "Offline")
            out.append(ModelRole(role=role, alias=alias, label=label, blurb=blurb, state=auto[0], state_label=auto[1],
                                 model_name="Chosen for each request",
                                 tech=hz.tech(alias=alias, router=served, raw_reason=reason)))
            continue
        state: Health
        if not have_caps and chain:     # "Checking" only until the first attempt; then honestly unknown
            state, state_label = "unknown", "Unknown" if checked else "Checking"
        elif not chain or cause == "not_deployed":
            state, state_label = "unknown", "Not installed"
            cause, cause_label = "not_deployed", "No model is installed for this role"
        elif available is False:
            state, state_label = ("paused", "Paused") if shed_hit else ("offline", "Offline")
        elif fallback:
            state, state_label = "degraded", "On backup model"
        elif degraded:
            state, state_label = "degraded", "Degraded"
        else:
            state, state_label = "healthy", "Ready"
        now_pid = served or primary
        out.append(ModelRole(
            role=role, alias=alias, label=label, blurb=blurb, state=state, state_label=state_label,
            model_name=hz.model_name(now_pid) if now_pid else "", physical_model=physical(now_pid, routing, caps),
            served_by=served, fallback_active=fallback, degraded=degraded, cause=cause, cause_label=cause_label,
            chain=[hz.model_name(p) for p in chain], canary=canary,
            tech=hz.tech(alias=alias, served_by=served, chain=", ".join(chain), raw_reason=reason,
                         min_params_b=floors.get(alias), shed_by_memory_guard=", ".join(sorted(shed_p & set(chain))),
                         note="Live status as seen by one gateway replica" if st else None)))
    return out


# Actions valid per lifecycle state (the controller re-checks every gate; this just hides dead buttons).
_STATE_ACTIONS: dict[str, tuple[ModelAction, ...]] = {
    "CANDIDATE": ("download", "pin", "block", "delete"),
    "STAGED": ("benchmark", "pin", "block", "delete"),
    "APPROVED": ("canary", "promote", "benchmark", "load", "pin", "block", "delete"),
    "CANARY": ("promote", "benchmark", "load", "unload"),
    "PRODUCTION": ("benchmark", "load", "unload", "pin", "unpin"),
    "STANDBY": ("promote", "canary", "benchmark", "load", "unload", "pin", "delete"),
    "REJECTED": ("benchmark", "delete", "block"),
    "FAILED": ("delete", "block"),
    "QUARANTINED": ("delete", "block"),
    "DISCOVERED": ("block", "delete"),
    "DEPRECATED": ("delete",),
}


def model_actions(state: str, device: str, pinned: bool, blocked: bool, used: bool) -> list[ModelAction]:
    acts = list(_STATE_ACTIONS.get(state.upper(), ()))
    if device.lower() == "gpu":         # GPU tiers need a gpusched command-job window the console can't open
        acts = [a for a in acts if a not in ("benchmark", "load")]
    if used:                            # the controller refuses to delete or stop a model a role still uses
        acts = [a for a in acts if a not in ("delete", "unload")]
    acts = [a for a in acts if not (a == "pin" and pinned) and not (a == "unpin" and not pinned)]
    if pinned and "unpin" not in acts and state.upper() != "PRODUCTION":
        acts.append("unpin")
    if blocked:
        acts = [a for a in acts if a != "block"] + ["unblock"]
    return acts


def benchmark_summary(b: dict[str, Any]) -> BenchmarkSummary:
    s = _d(b.get("summary"))
    return BenchmarkSummary(ts=hz.num(b.get("ts")) or 0.0, suite=str(b.get("suite") or ""),
                            quality=hz.num(s.get("quality")), ttft_ms_p50=hz.num(s.get("ttft_ms_p50")),
                            decode_tps_p50=hz.num(s.get("decode_tps_p50")), latency_ms_p50=hz.num(s.get("latency_ms_p50")),
                            errors=int(hz.num(s.get("errors")) or 0), items=int(hz.num(s.get("items")) or 0))


def build_deployment(row: dict[str, Any], routing: dict[str, Any], caps: dict[str, Any],
                     shed: list[str]) -> ModelDeployment:
    """Controller /v1/models row (+ live endpoint health) → ModelDeployment, with every number's basis labelled."""
    pid = str(row.get("id") or "")
    meta, prof, fit = _d(row.get("meta")), _d(row.get("profile")), _d(row.get("fit"))
    rprof = _d(_d(routing.get("profiles")).get(pid))
    prof = {**rprof, **prof}
    state = str(row.get("state") or "")
    label, health = hz.model_state(state)
    live = _d(_d(caps.get("profiles")).get(pid))
    is_shed = pid in shed_profiles(routing, shed) or deployment_name(prof) in set(shed)
    if state.upper() in ("PRODUCTION", "CANARY") and not _d(caps.get("profiles")):
        health, label = "unknown", f"{label} · live status unavailable"
    elif state.upper() in ("PRODUCTION", "CANARY", "STANDBY", "APPROVED") and live:
        if live.get("endpoint_healthy"):
            health = "healthy"
        elif is_shed:
            health, label = "paused", f"{label} · paused to free memory"
        elif state.upper() != "STANDBY":
            health, label = "offline", f"{label} · not answering"
    lb = _d(row.get("last_benchmark"))
    bsum = _d(lb.get("summary"))
    memory_gb, basis = None, "configured"
    if hz.num(prof.get("memory_budget_mb")):
        memory_gb = round(hz.num(prof["memory_budget_mb"]) / 1024, 2)
    elif hz.num(fit.get("total_mib")):
        memory_gb, basis = round(hz.num(fit["total_mib"]) / 1024, 2), "estimated"
    speed, ttft, sbasis = hz.num(bsum.get("decode_tps_p50")), hz.num(bsum.get("ttft_ms_p50")), None
    if speed is not None or ttft is not None:
        sbasis = "measured"
    elif hz.num(fit.get("est_cpu_decode_tps")) is not None:
        speed, sbasis = round(hz.num(fit["est_cpu_decode_tps"]), 1), "estimated"
    device = str(prof.get("device") or "")
    runtime = str(prof.get("runtime") or "")
    runtime_label = " ".join(x for x in ("llama.cpp" if runtime in ("llamacpp", "llama.cpp", "llama-server") else runtime,
                                         device.upper()) if x)
    roles = [str(a) for a in row.get("aliases") or [] if a] if isinstance(row.get("aliases"), list) else []
    pick = _d(meta.get("gguf_pick"))
    return ModelDeployment(
        id=pid, name=hz.model_name(pid or str(row.get("model_id") or "")), model_id=str(row.get("model_id") or ""),
        revision=str(row.get("revision") or prof.get("revision") or ""),
        source="Hugging Face" if (meta.get("source") in (None, "", "huggingface", "hf")) else str(meta.get("source")),
        runtime=runtime_label, device=device, precision=str(prof.get("precision") or pick.get("quant") or ""),
        params_b=hz.num(meta.get("params_b")) or hz.num(prof.get("params_b")),
        context=int(hz.num(prof.get("context")) or hz.num(meta.get("context_length")) or 0) or None,
        state=state, state_label=label, health=health, roles=roles, memory_gb=memory_gb, memory_basis=basis,  # type: ignore[arg-type]
        speed_tps=speed, ttft_ms=ttft, speed_basis=sbasis, quality=hz.num(bsum.get("quality")),  # type: ignore[arg-type]
        last_evaluated=hz.num(lb.get("ts")), pinned=bool(row.get("pinned")), blocked=bool(row.get("blocked")),
        actions=model_actions(state, device, bool(row.get("pinned")), bool(row.get("blocked")), bool(roles)),
        tech=hz.tech(profile=pid, state=state, reason=row.get("reason"), deployment=deployment_name(prof),
                     endpoint_healthy=live.get("endpoint_healthy") if live else None,
                     last_error=live.get("last_error") if live else None,
                     hardware_fit=hz.FIT.get(str(fit.get("verdict")), fit.get("verdict")),
                     memory_basis="configured budget" if basis == "configured" else "estimate from model size",
                     category=row.get("category"), shed_by_memory_guard=True if is_shed else None))


def _mib_gb(v: float | None) -> float | None:
    return round(v / 1024, 1) if v is not None else None


def _bytes_gb(v: float | None) -> float | None:
    return round(v / GIB, 1) if v is not None else None


def build_resource(gpu: dict[str, Any], prom: dict[str, list[dict[str, Any]]], now: float,
                   gpu_ok: bool) -> ResourceState:
    """What the DGX is doing. Memory is measured (node exporter) in GiB, not the nominal 128 GB."""
    state, label, why = hz.blerbz(gpu) if gpu else ("unknown", "Unknown", "GPU scheduler status hasn't been read yet.")
    pv = {k: upstream.prom_value(v) for k, v in prom.items() if k not in ("alerts", "waiting", "restarts",
                                                                          "terminated", "deploy_avail", "deploy_spec")}
    total = _bytes_gb(pv.get("mem_total"))
    avail = _bytes_gb(pv.get("mem_avail"))
    if avail is None and hz.num(gpu.get("mem_available_mib")):
        avail = _mib_gb(hz.num(gpu.get("mem_available_mib")))
    used = round(total - avail, 1) if total is not None and avail is not None else None
    res = pv.get("residents_mib")
    blerbz_gb = _mib_gb((res or 0) + (pv.get("leases_mib") or 0)) if res is not None else None
    lif_gb = _bytes_gb(pv.get("lif_mem"))
    # "Other" only when both named parts are measured; otherwise it would silently absorb the missing one.
    other = round(max(0.0, used - blerbz_gb - lif_gb), 1) \
        if used is not None and blerbz_gb is not None and lif_gb is not None else None
    t = pv.get("temp")
    temp, temp_note = "unknown", "No GPU temperature sensor is exported yet."
    if t is not None:
        temp = "normal" if t < 70 else "warm" if t < 85 else "hot"
        temp_note = f"Board sensor {t:.0f} °C; no GPU temperature sensor is exported yet."
    age = hz.num(gpu.get("age_sec"))
    return ResourceState(
        blerbz=state, blerbz_label=label, blerbz_reason=why, gpu_util_pct=hz.num(gpu.get("gpu_util_percent")),
        mem_total_gb=total, mem_used_gb=used, mem_available_gb=avail, blerbz_mem_gb=blerbz_gb,
        ai_serving_mem_gb=lif_gb, other_mem_gb=other, temperature=temp, temperature_note=temp_note,  # type: ignore[arg-type]
        stale=not gpu_ok or (age is not None and age > hz.GPUSCHED_STALE_SEC),
        updated_at=hz.num(gpu.get("ts")) or (now if gpu_ok else 0.0),
        tech=hz.tech(gpusched_state=gpu.get("state"), gpusched_reason=gpu.get("reason"),
                     p_next_hour=gpu.get("p_next_hour"), admissible_mib=gpu.get("admissible_mib"),
                     production_leases=gpu.get("production_leases"), holds=gpu.get("holds"),
                     residents_loaded=gpu.get("residents_loaded"), age_sec=gpu.get("age_sec"),
                     memory_units="GiB (1024³ bytes), measured by the node exporter",
                     blerbz_memory="gpusched resident models + active GPU leases" if blerbz_gb is not None else None))


def build_compute(r: ResourceState, prom_note: str | None) -> ComputeView:
    total = r.mem_total_gb
    work: list[WorkShare] = []

    def pct(gb: float | None) -> float | None:
        return round(gb / total * 100, 1) if gb is not None and total else None

    if r.blerbz_mem_gb is not None:
        work.append(WorkShare(key="blerbz", label="BLERBZ", gb=r.blerbz_mem_gb, pct=pct(r.blerbz_mem_gb)))
    if r.ai_serving_mem_gb is not None:
        work.append(WorkShare(key="ai_serving", label="AI Serving", gb=r.ai_serving_mem_gb, pct=pct(r.ai_serving_mem_gb)))
    if r.other_mem_gb is not None:
        work.append(WorkShare(key="other", label="Other", gb=r.other_mem_gb, pct=pct(r.other_mem_gb)))
    if r.mem_available_gb is not None:
        work.append(WorkShare(key="available", label="Available", gb=r.mem_available_gb, pct=pct(r.mem_available_gb)))
    note = prom_note or ("" if len(work) == 4 else "Memory split needs Prometheus data that isn't available; "
                                                    "showing what can be measured.")
    return ComputeView(resource=r, work=work, note=note)


def _kube(prom: dict[str, list[dict[str, Any]]], ns: str, dep: str) -> tuple[Health, str, list[TechDetail]] | None:
    """kube-state-metrics view of one Deployment (no RBAC needed): replicas + container waiting/terminated reasons."""
    def series(key: str) -> list[dict[str, Any]]:
        return [s for s in prom.get(key) or [] if isinstance(s, dict) and _d(s.get("metric")).get("namespace") == ns]

    def mine(s: dict[str, Any]) -> bool:
        m = _d(s.get("metric"))
        return m.get("deployment") == dep or str(m.get("pod") or "").startswith(f"{dep}-")

    avail = next((upstream.prom_value([s]) for s in series("deploy_avail") if mine(s)), None)
    spec = next((upstream.prom_value([s]) for s in series("deploy_spec") if mine(s)), None)
    waiting = [str(_d(s.get("metric")).get("reason")) for s in series("waiting") if mine(s)]
    restarted = {str(_d(s.get("metric")).get("pod")) for s in series("restarts") if mine(s)}
    terminated = [str(_d(s.get("metric")).get("reason")) for s in series("terminated")
                  if mine(s) and str(_d(s.get("metric")).get("pod")) in restarted]
    if avail is None and spec is None and not waiting:
        return None
    raw = hz.tech(namespace=ns, deployment=dep, replicas_available=avail, replicas_wanted=spec,
                  waiting_reason=", ".join(waiting), last_terminated=", ".join(terminated))
    if waiting:
        h, s = hz.k8s_state(waiting[0])
        return h, s, raw
    if terminated:
        h, s = hz.k8s_state(terminated[0])
        return h, s, raw
    if spec == 0:
        return "paused", "Stopped", raw
    if avail is not None and spec and avail < spec:
        return ("offline", "Not running") if avail == 0 else ("degraded", "Running with fewer copies than wanted"), raw
    return "healthy", "Running", raw


def build_services(src: dict[str, _Src], ov: dict[str, Any], gpu: dict[str, Any], routing: dict[str, Any],
                   caps: dict[str, Any], shed: list[str], prom: dict[str, list[dict[str, Any]]],
                   prom_ok: bool) -> list[ServiceHealth]:
    out: list[ServiceHealth] = []

    def add(key: str, health: Health, summary: str, impact: str | None = None,
            raw: list[TechDetail] | None = None, kube: tuple[str, str] | None = None, logs: bool = False) -> None:
        k = _kube(prom, *kube) if kube else None
        tech = list(raw or [])
        if k:
            tech += k[2]
            if health in ("offline", "unknown") and k[0] != "healthy":     # the pod state explains the outage
                health, summary = k[0], k[1]
        acts: list[Any] = ["retry"] + (["view_logs"] if logs else [])
        out.append(ServiceHealth(key=key, name=hz.SERVICE_NAMES.get(key, key), health=health, summary=summary,
                                 impact=impact if health not in ("healthy", "busy") else None, actions=acts, tech=tech))

    def down(name: str) -> tuple[Health, str]:
        s = src[name]
        if s.exc is not None and s.exc.unauthorized:
            return "attention", "Not connected: the console's access key is missing or was rejected"
        if not s.attempt_ts:
            return "unknown", "Checking…"
        return "offline", "Not answering"

    # gateway
    h = src["health"]
    gh = _d(h.value) if h.ok() else {}
    if gh:
        if gh.get("status") == "ok" and gh.get("routing_table", "controller") == "controller":
            add("gateway", "healthy", "Answering requests", raw=hz.tech(routing_table=gh.get("routing_table")),
                kube=("ai-system", "gateway"))
        elif gh.get("status") == "ok":
            add("gateway", "attention", "Answering, but using its built-in model list (control service unreachable)",
                "Model changes won't reach the gateway until it reconnects.", hz.tech(routing_table=gh.get("routing_table")))
        else:
            add("gateway", "degraded", "Running, but no general chat model is available",
                upstream.SERVICE_IMPACT["gateway"], hz.tech(status=gh.get("status")))
    else:
        add("gateway", *down("health"), upstream.SERVICE_IMPACT["gateway"], hz.tech(error=h.error),
            kube=("ai-system", "gateway"))
    # controller: any controller source answering counts
    c_ok = src["gpu"].ok() or src["overview"].ok()
    if c_ok:
        up = hz.num(ov.get("controller_uptime_sec"))
        add("controller", "healthy", "Managing models", raw=hz.tech(uptime_sec=up), kube=("ai-system", "controller"),
            logs=True)
    else:
        add("controller", *down("gpu"), upstream.SERVICE_IMPACT["controller"], hz.tech(error=src["gpu"].error),
            kube=("ai-system", "controller"), logs=True)
    # decision service (reported inside the controller overview)
    df = ov.get("decision_fabric") if ov else None
    if not ov:
        add("decision", "unknown", "Unknown while the control service isn't answering")
    elif not isinstance(df, dict) or "error" in df:
        add("decision", "offline", "Not answering", "Requests use built-in routing rules; decision reviews can't be read.",
            hz.tech(error=(df or {}).get("error") if isinstance(df, dict) else None), kube=("ai-system", "decision-fabric"))
    elif df.get("jev_breaker_open"):
        add("decision", "degraded", "Using rules only: Jev is paused after errors",
            "Routing and reviews use built-in rules until Jev recovers.", hz.tech(provider=df.get("provider")))
    elif not df.get("jev_enabled", True):
        add("decision", "healthy", "Running on rules only (Jev turned off)", raw=hz.tech(provider=df.get("provider")))
    else:
        add("decision", "healthy", "Deciding with Jev and rules", raw=hz.tech(provider=df.get("provider")))
    # batch
    b = ov.get("batch") if ov else None
    if not ov:
        add("batch", "unknown", "Unknown while the control service isn't answering")
    elif not isinstance(b, dict) or "error" in b:
        add("batch", "offline", "Not answering", upstream.SERVICE_IMPACT["batch"],
            hz.tech(error=(b or {}).get("error") if isinstance(b, dict) else None), kube=("ai-system", "batch"))
    elif b.get("paused"):
        why = hz.job_reason(str(b.get("paused_reason") or b.get("pause_reason") or "")) or "Paused by an operator."
        add("batch", "paused", f"Paused: {why.rstrip('.')}", "Queued batch work waits until batch is resumed.",
            hz.tech(paused_reason=b.get("paused_reason")))
    else:
        # running/pending count items (requests inside batch jobs), not jobs: say so, Home counts jobs
        add("batch", "healthy", f"{int(hz.num(b.get('running')) or 0):,} items running, "
                                f"{int(hz.num(b.get('pending')) or 0):,} waiting",
            raw=hz.tech(blerbz=_d(b.get("blerbz")).get("state")))
    # GPU scheduler (through the controller)
    if gpu:
        st = hz.blerbz(gpu)[0]
        if st == "unknown":
            add("gpusched", "attention", "Not reporting; background work is held to be safe",
                "Batch and evaluations wait; local AI answers one request at a time.", hz.tech(reason=gpu.get("reason")))
        else:
            holds = int(hz.num(gpu.get("holds")) or 0)
            add("gpusched", "healthy", f"Reporting · holding {holds} GPU admission{'s' if holds != 1 else ''}"
                if holds else "Reporting", raw=hz.tech(state=gpu.get("state"), age_sec=gpu.get("age_sec")))
    else:
        add("gpusched", "unknown", "Unknown while the control service isn't answering")
    # Prometheus
    if prom_ok:
        add("prometheus", "healthy", "Recording history")
    else:
        add("prometheus", *down("prom"), upstream.SERVICE_IMPACT["prometheus"], hz.tech(error=src["prom"].error))
    # knowledge
    if not settings.knowledge_url():
        add("knowledge", "healthy", "Built-in read-only knowledge (no knowledge service configured)")
    elif src["knowledge"].ok():
        add("knowledge", "healthy", "Answering")
    else:
        add("knowledge", *down("knowledge"), upstream.SERVICE_IMPACT["knowledge"], hz.tech(error=src["knowledge"].error),
            kube=("ai-system", "knowledge"))
    _earn_services(add, src, prom)
    # model servers (one per routing profile), translated through the guard and kube-state
    profiles = _d(routing.get("profiles"))
    live = _d(caps.get("profiles"))
    sp = shed_profiles(routing, shed)
    for pid in sorted(set(profiles) | set(live)):
        lp, rp = _d(live.get(pid)), _d(profiles.get(pid))
        dep = deployment_name(rp)
        name = f"{hz.model_name(pid)} server"
        raw = hz.tech(profile=pid, deployment=dep, last_error=lp.get("last_error"))
        kube = ("ai-serving", dep) if dep else None
        if pid in sp:
            hs: tuple[Health, str] = ("paused", "Paused to free memory for BLERBZ")
            impact = "Its roles use a backup model until memory recovers; it restarts automatically."
        elif not lp:
            hs, impact = ("unknown", "Unknown (no live status)" if live else
                          "Unknown while the AI gateway and control service aren't answering"), None
        elif lp.get("endpoint_healthy"):
            hs, impact = ("healthy", "Answering"), None
        else:
            hs, impact = ("offline", "Not answering"), "Roles that use it fall back to a backup model."
        before = len(out)
        add(f"model:{pid}", hs[0], hs[1], impact, raw, kube=kube if hs[0] != "paused" else None, logs=True)
        out[before] = out[before].model_copy(update={"name": name})
    return out


def _earn_services(add: Callable[..., None], src: dict[str, _Src], prom: dict[str, list[dict[str, Any]]]) -> None:
    """The earning system (namespace earn): its runtime, research worker and nightly backup."""
    e = src["earn"]
    kube = ("earn", "earn")
    st = _d(e.value) if e.ok() else {}
    if st:
        h, summary, impact, raw = earn.service(st)
        add("earn", h, summary, impact, raw, kube=kube)
    elif settings.earn_url() and e.exc is not None and e.exc.unauthorized:
        add("earn", "attention", "Not connected: the console's Earn read key is missing or was rejected",
            "Earn's trading state can't be shown; its pods are still watched.", hz.tech(error=e.error), kube=kube)
    elif settings.earn_url() and e.attempt_ts:
        add("earn", "offline", "Not answering", upstream.SERVICE_IMPACT["earn"], hz.tech(error=e.error), kube=kube)
    elif (k := _kube(prom, *kube)) is not None:
        add("earn", k[0], k[1], None if k[0] == "healthy" else upstream.SERVICE_IMPACT["earn"], k[2])
    if (k := _kube(prom, "earn", "earn-synth")) is not None:
        add("earn-synth", k[0], "Running" if k[0] == "healthy" else k[1],
            None if k[0] == "healthy" else "Earn's offline price forecasts pause; trading is unaffected.", k[2])
    if prom.get("earn_backup_ok") is not None and (k := _kube(prom, *kube)) is not None:
        h, summary, raw = earn.backup(upstream.prom_value(prom["earn_backup_ok"]), time.time())
        add("earn-backup", h, summary, "Restore would lose more than a day of Earn's ledger." if h != "healthy" else None,
            raw)


def build_approvals(human: Any) -> list[Approval]:
    """Pending decision-review tickets → minimal Approvals (the agents route renders the full card)."""
    q = human.get("queue") if isinstance(human, dict) else None
    out: list[Approval] = []
    for t in q if isinstance(q, list) else []:
        if not isinstance(t, dict) or t.get("status") != "pending":
            continue
        pkg = _d(t.get("package"))
        crit = _d(pkg.get("criteria"))
        labels = [str(x) for x in pkg.get("labels") or []] if isinstance(pkg.get("labels"), list) else []
        ref = str(t.get("decision_ref") or pkg.get("decision") or "")
        reason = str(pkg.get("reason") or "")
        out.append(Approval(
            id=f"review:{t.get('id')}", kind="review", title=f"Review: {ref.split('/')[0].replace('-', ' ')}",
            action=str(pkg.get("instructions") or "Choose the right answer")[:300],
            why=hz.REVIEW_REASONS.get(reason, reason.replace("_", " ") or "A person's answer was requested"),
            impact="Nothing is waiting on this answer; it improves future decisions.",
            options=[ApprovalOption(value=lb, label=lb.replace("_", " ").capitalize(),
                                    description=str(crit.get(lb)) if crit.get(lb) else None) for lb in labels],
            created_at=hz.num(t.get("ts")) or 0.0, status="pending", blocking=False,
            tech=hz.tech(ticket=t.get("id"), decision_ref=ref, reason=reason, data_class=pkg.get("data_class"))))
    return out


def build_jobs(ov: dict[str, Any], rows: list[dict[str, Any]], now: float) -> JobsSummary:
    b = _d(ov.get("batch"))
    jobs = _d(b.get("jobs"))
    n = {k: int(hz.num(v) or 0) for k, v in jobs.items()}
    running, queued, waiting = n.get("running", 0), n.get("queued", 0), n.get("paused", 0)
    if b.get("paused"):
        waiting, queued = waiting + queued, 0
    failed = 0
    for j in b.get("recent") or [] if isinstance(b.get("recent"), list) else []:
        if isinstance(j, dict) and j.get("state") in ("failed", "expired") and (hz.num(j.get("finished")) or 0) > now - 86400:
            failed += 1
    for v in _d(ov.get("tasks")).values():
        if v == "running":          # downloads, benchmarks and discovery are jobs too (Jobs lists them)
            running += 1
        # A failed controller task has no timestamp and stays until the controller restarts, so it can't
        # be counted as "failed in the last 24 h"; Jobs still lists it.
    failed += sum(1 for r in rows if r.get("kind") == "discovery_failed" and (hz.num(r.get("ts")) or 0) > now - 86400)
    return JobsSummary(running=running, queued=queued, waiting=waiting, failed_24h=failed)


def _not_connected(src: dict[str, _Src]) -> dict[str, str]:
    """Upstream → sentence, for upstreams that reject (or were never given) the console's key. Until the owner
    creates the keys and rolls out the services, only the open endpoints answer: a setup step, not an outage."""
    out: dict[str, str] = {}
    ctrl = ("gpu", "overview", "activity", "models", "human")
    if any(src[n].exc is not None and src[n].exc.unauthorized for n in ctrl):
        why = "its access key isn't configured" if not settings.admin_key() else "its access key was rejected"
        out["controller"] = f"The console isn't connected to the model control service yet ({why})"
    if not settings.gateway_key():
        out["gateway"] = "The console isn't connected to the AI gateway yet (its access key isn't configured), so Ask can't answer"
    elif src["capabilities"].exc is not None and src["capabilities"].exc.unauthorized:
        out["gateway"] = "The console isn't connected to the AI gateway yet (its access key was rejected), so Ask can't answer"
    return out


def fresh(name: str, now: float) -> Any:
    """A source's last good payload while it is recent enough to describe the present, else None.
    Live state (who answers, GPU owner, pending reviews) must not outlive its upstream: a cached
    'Ready' would keep saying so while every upstream is offline."""
    s = _st.src[name]
    every = SOURCES[name][0]
    if s.value is None or now - s.ok_ts > max(3 * every, 3 * settings.poll_sec(), 30.0):
        return None
    return s.value


def build(now: float) -> Snapshot:
    """Assemble a Snapshot from the source caches (pure apart from reading module state)."""
    src = _st.src
    gpu_src, ov_src = src["gpu"], src["overview"]
    ov = _d(ov_src.value) if ov_src.value is not None and ov_src.ok_ts > now - 120 else {}
    gpu = _d(fresh("gpu", now)) or _d(ov.get("blerbz"))
    gpu_ok = gpu_src.ok() or (ov_src.ok() and bool(_d(ov.get("blerbz"))))
    routing = _d(src["routing"].value)      # configuration (chains, profiles): fine to keep while stale
    # Capabilities: the controller's overview copy is refreshed every cycle; the direct gateway call is the
    # fallback (and the only source when the controller is down). Neither is used once it is stale.
    caps = _d(ov.get("capabilities")) if ov_src.ok() else {}
    if not caps or (src["capabilities"].ok() and src["capabilities"].ok_ts > ov_src.ok_ts):
        caps = _d(fresh("capabilities", now)) or caps
    guard = _d(ov.get("memory_guard"))
    shed = [str(x) for x in guard.get("shed") or []] if isinstance(guard.get("shed"), list) else []
    settings_ = _d(ov.get("settings"))
    prom_src = src["prom"]
    prom: dict[str, list[dict[str, Any]]] = prom_src.value if prom_src.ok() and isinstance(prom_src.value, dict) else {}
    prom_ok = prom_src.ok()
    health_v = _d(src["health"].value) if src["health"].ok() else None

    roles = build_roles(routing, caps, shed, checked=bool(ov_src.attempt_ts or src["capabilities"].attempt_ts))
    resource = build_resource(gpu, prom, now, gpu_ok)
    compute = build_compute(resource, None if prom_ok else
                            "Prometheus isn't answering, so the memory split and temperature are unavailable.")
    rows = [_st.rows[k] for k in sorted(_st.rows, reverse=True)]
    activity = [e for e in (hz.activity(r) for r in rows) if e is not None]
    models_v = _d(src["models"].value)
    deployments = [build_deployment(r, routing, caps, shed) for r in models_v.get("models") or []
                   if isinstance(r, dict)] if isinstance(models_v.get("models"), list) else []
    # Service health speaks only for what answered in the last cycle: a cached overview, GPU state or
    # capabilities list would otherwise keep reporting "healthy"/"Answering" for services behind an
    # upstream that is down (roles and resources keep the cache and say it is stale instead).
    caps_live = caps if (ov_src.ok() or src["capabilities"].ok()) else {}
    services = build_services(src, ov if ov_src.ok() else {}, gpu if gpu_ok else {}, routing, caps_live, shed, prom,
                              prom_ok)
    approvals = build_approvals(fresh("human", now))
    jobs = build_jobs(ov, rows, now)
    alerts = [a for a in prom.get("alerts") or [] if isinstance(a, dict)]
    mem_avail_mib = (resource.mem_available_gb * 1024) if resource.mem_available_gb is not None else None
    maintenance = bool(settings_.get("maintenance"))
    batch_paused = bool(settings_.get("batch_paused") or _d(ov.get("batch")).get("paused"))
    not_connected = _not_connected(src)
    controller_ok = gpu_src.ok() or ov_src.ok() or not (gpu_src.attempt_ts or ov_src.attempt_ts) or \
        "controller" in not_connected
    health, headline, reasons = hz.system_health(
        gateway_health=health_v if src["health"].attempt_ts else {"status": "ok", "useful_local_ai": True},
        controller_ok=controller_ok, gpu=gpu, mem_available_mib=mem_avail_mib, shed=shed, roles=roles, alerts=alerts,
        maintenance=maintenance, batch_paused=batch_paused, decision=_d(ov.get("decision_fabric")),
        not_connected=list(not_connected.values()))
    if not src["health"].attempt_ts and not gpu_src.attempt_ts:
        health, headline = "unknown", "Checking Labzilla…"
    la, la_label = hz.local_ai(health_v, roles, resource.blerbz) if src["health"].attempt_ts else ("unknown", "Checking…")
    by = {r.role: r for r in roles}
    tasks = _d(ov.get("tasks"))
    agents_running = sum(1 for k, v in tasks.items() if v == "running" and (k == "discovery" or k.startswith("benchmark:")))
    errors = {k: s.error for k, s in src.items() if s.error}
    _notify(now, roles, resource, mem_avail_mib, shed, services, approvals, gpu, ov)
    status = SystemStatus(
        health=health, headline=headline, local_ai=la, local_ai_label=la_label,
        primary_model=by["balanced"].model_name or None if "balanced" in by else None,
        fast_model=by["fast"].model_name or None if "fast" in by else None,
        resource=resource, agents_running=agents_running, jobs=jobs, approvals_pending=len(approvals),
        notifications=_st.notifier.current(now), services=services, maintenance=maintenance, batch_paused=batch_paused,
        connection=ConnectionInfo(local=True, secure=settings.public_url().startswith("https://"), url=settings.public_url()),
        updated_at=now,
        tech=[TechDetail(label=h, value=s) for h, s in reasons] + hz.tech(**{f"{k} unavailable": v for k, v in errors.items()}))
    raw = {"health": health_v, "gpu": gpu, "overview": ov, "routing": routing, "capabilities": caps,
           "batch_stats": _d(ov.get("batch")), "settings": settings_, "memory_guard": guard, "tasks": tasks,
           "models": models_v.get("models") if isinstance(models_v.get("models"), list) else [],
           "human": src["human"].value, "prom": prom, "alerts": alerts, "activity_rows": rows,
           "earn": src["earn"].value if src["earn"].ok() else None}
    return Snapshot(status=status, compute=compute, roles=roles, deployments=deployments, activity=activity,
                    services=services, approvals=approvals, jobs=jobs, raw=raw, errors=errors, updated_at=now)


# ── notifications + hub publishing ──────────────────────────────────────────────────────────

def _notify(now: float, roles: list[ModelRole], r: ResourceState, mem_avail_mib: float | None, shed: list[str],
            services: list[ServiceHealth], approvals: list[Approval], gpu: dict[str, Any], ov: dict[str, Any]) -> None:
    n = _st.notifier
    for role in roles:
        if role.role in ("fast", "balanced"):
            on = role.fallback_active and role.cause != "canary"
            n.condition(f"fallback:{role.role}", on, "fallback", "warning", f"{role.label} model unavailable",
                        f"Labzilla is using {role.model_name or 'a backup model'} instead. Responses may be less capable."
                        + (f" {role.cause_label}." if role.cause_label else ""), f"/models/roles/{role.role}", now)
    low = mem_avail_mib is not None and mem_avail_mib < hz.MEM_HEADROOM_MIB
    body = (f"{mem_avail_mib / 1024:.1f} GB free, below the 8 GB safety margin." if low else "") + \
           (f" Paused to free memory: {', '.join(hz.service_name(s) for s in shed)}." if shed else "")
    n.condition("memory", low or bool(shed), "memory_pressure", "warning", "Memory is tight", body.strip(),
                "/system/compute", now)
    for s in services:
        if s.key in CORE_SERVICES:
            n.condition(f"service:{s.key}", s.health == "offline", "service_down", "error", f"{s.name} unavailable",
                        s.impact or s.summary, "/system/services", now)
    pending = len(approvals)
    n.condition("approvals", pending > 0, "approval", "info",
                f"{pending} decision review{'s' if pending != 1 else ''} waiting",
                "Agents asked for a person's check. Nothing is blocked while they wait.", "/agents", now)
    b = _d(ov.get("batch"))
    held = int(hz.num(b.get("pending")) or 0)
    contention = r.blerbz == "busy" and bool(gpu.get("production_live")) and (held > 0 or bool(shed) or any(
        x.cause == "yielded_to_primary" for x in roles))
    n.condition("blerbz", contention, "blerbz_contention", "info", "BLERBZ is using the GPU",
                (f"{held} batch job item{'s' if held != 1 else ''} waiting; " if held else "")
                + "local AI is slower and work resumes automatically when BLERBZ finishes.", "/system/compute", now)
    # events from activity rows that arrived since the last build
    for row in _st.new_rows:
        kind, d = row.get("kind"), _d(row.get("detail"))
        subj = str(row.get("subject") or "")
        if kind == "comparison_complete" and str(d.get("recommendation")).upper() == "CANARY":
            n.event(f"candidate:{row.get('seq')}", "candidate", "success", "New model candidate ready",
                    f"{hz.model_name(subj)} beat the current model in testing.",
                    f"/models/candidates/{quote(subj, safe='')}", now)
        elif kind == "discovery_finished":
            ev = hz.activity(row)
            n.event(f"job:discovery:{row.get('seq')}", "job_done", "success", ev.title if ev else "Model check finished",
                    "Open Models to see the results.", "/models/discovery", now)
    # batch jobs reaching a terminal state while we watched
    for j in b.get("recent") or [] if isinstance(b.get("recent"), list) else []:
        if not isinstance(j, dict) or not j.get("id"):
            continue
        jid, state = str(j["id"]), str(j.get("state") or "")
        before = _st.jobs_seen.get(jid)
        _st.jobs_seen[jid] = state
        terminal = state in ("completed", "failed", "expired")
        fresh = (before is not None and before not in ("completed", "failed", "expired", "cancelled")) or \
                (before is None and (hz.num(j.get("finished")) or 0) > _st.started_at)
        if terminal and fresh:
            title = str(j.get("description") or "Batch job")[:80]
            ok = state == "completed"
            n.event(f"job:batch:{jid}:{state}", "job_done", "success" if ok else "error",
                    f"Batch job {'finished' if ok else 'failed'}: {title}",
                    hz.job_reason(str(j.get("reason") or "")) or ("All items processed." if ok else "Open Jobs for details."),
                    f"/jobs/batch:{jid}", now)
    if len(_st.jobs_seen) > 500:
        for k in list(_st.jobs_seen)[:-200]:
            del _st.jobs_seen[k]


def _fp(m: Any) -> Any:
    """Fingerprint of a contract without timestamps, so 'changed' means a human-visible change."""
    def strip(x: Any) -> Any:
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if k not in ("updated_at", "ts", "age_sec", "uptime_sec")}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x
    return strip(m.model_dump() if hasattr(m, "model_dump") else m)


def _publish(prev: Snapshot, new: Snapshot) -> None:
    keys = _st.keys
    k = _fp(new.status)
    if keys.get("status") != k:
        keys["status"] = k
        hub.publish("status", new.status)
    for row in sorted(_st.new_rows, key=lambda r: r.get("seq") or 0):
        ev = hz.activity(row)
        if ev is not None and _st.notifier.seeded:
            hub.publish("activity", ev)
    _st.new_rows = []
    for nt in _st.notifier.outbox:
        hub.publish("notification", nt)
    _st.notifier.outbox = []
    roles_prev = {r.role: (r.state, r.served_by, r.fallback_active, r.cause) for r in prev.roles}
    for r in new.roles:
        cur = (r.state, r.served_by, r.fallback_active, r.cause)
        if prev.updated_at and roles_prev.get(r.role) not in (None, cur):
            hub.publish("model", ModelEvent(role=r.role, deployment_id=r.served_by,
                                            summary=f"{r.label}: {r.state_label.lower()}"
                                                    + (f" ({r.cause_label})" if r.cause_label else "")))
    # The registry changed (a check shortlisted a candidate, a download or benchmark moved a model on): Models
    # and candidate screens refetch, so a finished check shows its candidates without a reload.
    mk = sorted((str(m.get("id")), str(m.get("state"))) for m in new.raw.get("models") or [] if isinstance(m, dict))
    if keys.get("models") != mk:
        had = "models" in keys
        keys["models"] = mk
        if prev.updated_at and had:
            hub.publish("model", ModelEvent(summary="Model registry changed"))
    seen = {a.id for a in prev.approvals}
    if prev.updated_at:
        for a in new.approvals:
            if a.id not in seen:
                hub.publish("approval", a)
    jk = new.jobs.model_dump()
    if keys.get("jobs") != jk:
        keys["jobs"] = jk
        if prev.updated_at:
            hub.publish("jobs", JobsEvent(summary=new.jobs))


# ── shared helper for operator switches (also used by /api/jobs/batch/pause-all) ─────────────

async def set_setting(key: str, value: Any, actor: str | None) -> dict[str, Any]:
    """POST one controller setting, working around its pause propagation.

    The controller recomputes batch pause as `body.batch_paused or body.maintenance` from the request
    body only (controller app.py set_settings), so un-pausing batch while maintenance is on would
    silently resume batch. When one of the two is posted, the other is included if it is currently on.
    """
    body: dict[str, Any] = {key: value}
    if key in ("batch_paused", "maintenance"):
        other = "maintenance" if key == "batch_paused" else "batch_paused"
        current = await upstream.get("controller", "/v1/settings")
        if isinstance(current, dict) and current.get(other):
            body[other] = True
            note_echo(other)
    out = await upstream.post("controller", "/v1/settings", body, actor=actor)
    refresh_soon()
    return out if isinstance(out, dict) else {}
