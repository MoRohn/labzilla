"""LIF controller: model registry, lifecycle, discovery, routing table, availability, Control Center.

Control plane only. If this service is down, the gateway keeps serving from its
last-known-good routing table; nothing in the data plane depends on it being up.

Auth: every /v1/* route except /v1/routing (in-cluster, NetworkPolicy-restricted) needs
`Authorization: Bearer <admin key>` (Secret lif-admin-keys, `name:key` lines).
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from lif.common import config, log, metrics
from lif.controller.k8s import K8s
from lif.controller.autopromote import AutoPromoter
from lif.controller.lifecycle import Lifecycle, OpError
from lif.decision.dag import DagRuntime
from lif.decision.fabric import DecisionFabric
from lif.decision.providers import JevProvider
from lif.decision.rules import rules
from lif.decision.types import load_definitions
from lif.gpu.state import GpuStateWatcher
from lif.models.discovery import Discovery
from lif.models.registry import Registry, RegistryError

LOG = log.get("lif.controller")
UI_DIR = Path(__file__).resolve().parents[2] / "apps" / "control-center"


class State:
    def __init__(self):
        self.reg = Registry(os.environ.get("LIF_REGISTRY_DB", "/data/registry.db"))
        self.gpu = GpuStateWatcher()
        self.fabric = DecisionFabric(load_definitions(), rules, jev=JevProvider(config.secret("TYPE_SAFE_JEV_API_KEY")))
        self.dag = DagRuntime(self.fabric)
        self.dag.load()
        self.k8s = K8s()
        self.life = Lifecycle(self.reg, self.k8s, self.gpu, self.fabric)
        self.auto = AutoPromoter(self.life, lambda q: _prom(q))
        self.disc = Discovery(self.reg, self.dag, admissible_mib=lambda: self.gpu.current().admissible_mib,
                              observer=self._observe if (config.get("decision_engineering.observe") or {}).get(
                                  "discovery") else None)
        self.admin = config.keys("LIF_ADMIN_KEYS")
        self.gateway_url = os.environ.get("LIF_GATEWAY_URL", "http://gateway.ai-system.svc:8080")
        self.gateway_key = config.secret("LIF_PROBE_KEY") or ""
        self.decision_url = os.environ.get("LIF_DECISION_URL", "http://decision-fabric.ai-system.svc:8080")
        self.batch_url = os.environ.get("LIF_BATCH_URL", "http://batch.ai-system.svc:8080")
        self.http = httpx.AsyncClient(timeout=30)
        self.tasks: list[asyncio.Task] = []
        self.started = time.time()

    async def _observe(self, payload: dict) -> None:
        """Report a discovery decision to the decision-fabric's shadow API (POST /de/observe)."""
        r = await self.http.post(f"{self.decision_url}/de/observe", json=payload, timeout=20)
        r.raise_for_status()


S: State


async def _loop(fn, every: float, name: str):
    while True:
        try:
            await fn()
        except Exception:
            LOG.exception(f"{name} loop failed")
        await asyncio.sleep(every)


# ── availability probes: real inference, not process health ───────────────────

PROBES = {
    "gateway": ("GET", "/v1/health", None),
    "fast_model": ("POST", "/v1/chat/completions",
                   {"model": "local/fast", "messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 2,
                    "temperature": 0.01}),
    "default_model": ("POST", "/v1/chat/completions",
                      {"model": "local/default", "messages": [{"role": "user", "content": "Say OK."}],
                       "max_tokens": 2, "temperature": 0.01}),
    "embedding": ("POST", "/v1/embeddings", {"model": "local/embedding", "input": "availability probe"}),
}


async def probe_all() -> None:
    ts = int(time.time())
    results = {}
    for cap, (method, path, body) in PROBES.items():
        t0 = time.perf_counter()
        try:
            r = await S.http.request(method, f"{S.gateway_url}{path}", json=body, timeout=60,
                                     headers={"Authorization": f"Bearer {S.gateway_key}",
                                              "X-LIF-Workload": "availability-probe"})
            ok = r.status_code == 200
            if ok and cap == "default_model":
                # served by a fallback still counts as useful AI, but not as default availability
                ok = not (r.json().get("lif") or {}).get("fallback", False)
        except Exception:
            ok = False
        dt = (time.perf_counter() - t0) * 1000
        results[cap] = ok
        S.reg.record_availability(cap, ok, round(dt, 1), ts)
        metrics.available.labels(cap).set(int(ok))
        metrics.probe_latency.labels(cap).set(dt / 1000)
    # decision fabric: the service answers a rules-backed decision (works without Jev)
    try:
        r = await S.http.post(f"{S.decision_url}/decision/evaluate", timeout=10,
                              json={"decision": "batch-priority", "state": {"deadline_hours": 1}})
        dok = r.status_code == 200
    except Exception:
        dok = False
    S.reg.record_availability("decision_fabric", dok, None, ts)
    metrics.available.labels("decision_fabric").set(int(dok))
    useful = results.get("fast_model") or results.get("default_model")
    S.reg.record_availability("useful_local_ai", bool(useful), None, ts)
    metrics.available.labels("useful_local_ai").set(int(bool(useful)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global S
    log.setup()
    S = State()
    S.life.seed()
    await S.gpu.refresh()
    S.tasks = [asyncio.create_task(S.gpu.run()),
               asyncio.create_task(_loop(S.life.protect, 5, "protect")),
               asyncio.create_task(_loop(probe_all, 30, "probe")),
               asyncio.create_task(_loop(S.auto.tick, 60, "auto-promotion")),
               asyncio.create_task(_loop(lambda: _daily(), 3600, "daily"))]
    S.reg.event("controller_started", "", "system")
    try:
        yield
    finally:
        for t in S.tasks:
            t.cancel()
        await asyncio.gather(*S.tasks, return_exceptions=True)
        await S.http.aclose()


async def _daily() -> None:
    S.reg.prune_availability()
    last = S.reg.setting("last_gc", 0)
    if time.time() - last > 86400:
        if S.k8s.enabled:
            await S.life.gc()
        S.reg.set_setting("last_gc", time.time(), actor="retention")     # only once it ran: a failure retries
    if S.reg.setting("automatic_discovery", config.get("models.automatic_discovery")):
        last_d = S.reg.setting("last_auto_discovery", 0)
        if time.time() - last_d > 7 * 86400:
            await S.disc.run(actor="scheduler")
            S.reg.set_setting("last_auto_discovery", time.time(), actor="scheduler")


app = FastAPI(title="LIF controller", lifespan=lifespan)
OPEN = {"/healthz", "/metrics", "/v1/routing", "/"}


@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    if path in OPEN or path.startswith("/ui"):
        return await call_next(request)
    h = request.headers.get("authorization", "")
    who = S.admin.get(h[7:].strip()) if h.lower().startswith("bearer ") else None
    if who is None:
        return JSONResponse({"error": "admin key required"}, status_code=401)
    request.state.actor = "operator"
    request.state.who = who
    return await call_next(request)


def _ok(x):
    return JSONResponse(x)


async def _op(fn, *a, **kw):
    try:
        out = fn(*a, **kw)
        if asyncio.iscoroutine(out):
            out = await out
        return JSONResponse(out if out is not None else {"ok": True})
    except (OpError, RegistryError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except KeyError as e:
        return JSONResponse({"error": f"not found: {e}"}, status_code=404)


# ── basics ───────────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/metrics")
async def prom():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/v1/routing")
async def routing():
    return S.life.routing_table()


@app.get("/")
async def root():
    return FileResponse(UI_DIR / "index.html") if (UI_DIR / "index.html").exists() else _ok({"ui": "not built"})


@app.get("/ui/{path:path}")
async def ui(path: str):
    p = (UI_DIR / path).resolve()
    if UI_DIR.resolve() not in p.parents or not p.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p)


# ── overview ─────────────────────────────────────────────────────────────────

async def _get_json(url: str, **kw) -> dict:
    try:
        r = await S.http.get(url, timeout=5, **kw)
        return r.json() if r.status_code == 200 else {"error": r.status_code}
    except Exception as e:
        return {"error": str(e)[:120]}


@app.get("/v1/overview")
async def overview():
    snap = S.gpu.current()
    caps = await _get_json(f"{S.gateway_url}/v1/capabilities", headers={"Authorization": f"Bearer {S.gateway_key}"})
    dfs = await _get_json(f"{S.decision_url}/decision/status")
    batch = await _get_json(f"{S.batch_url}/v1/batch/stats")
    return {"blerbz": snap.to_dict(), "capabilities": caps, "decision_fabric": dfs, "batch": batch,
            "availability_24h": S.reg.availability(86400), "availability_7d": S.reg.availability(7 * 86400),
            "settings": _settings(), "tasks": S.life.task_status(), "memory_guard": S.life.guard_status(),
            "models": {s: len(S.reg.list([s])) for s in ("PRODUCTION", "CANARY", "CANDIDATE", "STAGED",
                                                          "APPROVED", "STANDBY", "REJECTED")},
            "controller_uptime_sec": round(time.time() - S.started)}


# ── Decision Engineering (proxied to the decision-fabric service, /de/*) ───────
# Reads pass through. Lifecycle changes carry the internal key, are attributed to the
# authenticated admin (never a client-supplied name) and land in the activity timeline.
_DE_MUTATING = ("transition", "rollback", "human/", "inventory")


@app.api_route("/v1/de/{path:path}", methods=["GET", "POST"])
async def decision_engineering(path: str, request: Request):
    url = f"{S.decision_url}/de/{path}"
    headers: dict[str, str] = {}
    body = None
    if request.method == "POST":
        body = await request.json()
        if path.startswith(_DE_MUTATING):
            headers["X-LIF-Internal"] = config.secret("LIF_INTERNAL_KEY") or ""
            if isinstance(body, dict) and path.startswith(("transition", "rollback", "human/")):
                body["actor"] = body["reviewer"] = getattr(request.state, "who", "operator")
    try:
        r = await S.http.request(request.method, url, json=body, headers=headers, params=dict(request.query_params),
                                 timeout=120)
    except Exception as e:
        return JSONResponse({"error": f"decision-fabric unreachable: {str(e)[:120]}"}, status_code=502)
    if request.method == "POST" and path.startswith(_DE_MUTATING) and r.status_code < 300:
        S.reg.event(f"decision_{path.split('/')[0]}", str((body or {}).get("ref") or (body or {}).get("name") or path),
                    getattr(request.state, "who", "operator"),
                    **{k: v for k, v in (body or {}).items() if k in ("stage", "reason", "policy", "rollout_pct",
                                                                    "answer")})
    try:
        data = r.json()
    except ValueError:
        data = {"error": r.text[:300]}
    return JSONResponse(data, status_code=r.status_code)


PROM = os.environ.get("LIF_PROMETHEUS_URL", "http://monitoring-kube-prometheus-prometheus.monitoring.svc:9090")


async def _prom(q: str) -> dict[str, float]:
    try:
        r = await S.http.get(f"{PROM}/api/v1/query", params={"query": q}, timeout=10)
        out = {}
        for s in r.json()["data"]["result"]:
            key = ",".join(f"{v}" for k, v in sorted(s["metric"].items())) or "value"
            out[key] = float(s["value"][1])
        return out
    except Exception:
        return {}


@app.get("/v1/savings")
async def savings(window: str = "24h"):
    """LOCAL AI VALUE. Counts are measured (Prometheus); every $ figure is an ESTIMATE."""
    if window not in ("1h", "24h", "7d", "30d"):
        return JSONResponse({"error": "window must be 1h|24h|7d|30d"}, status_code=400)
    hours = {"1h": 1, "24h": 24, "7d": 168, "30d": 720}[window]
    tiers = await _prom(f"sum by (tier) (increase(lif_tasks_total{{workload!='availability-probe'}}[{window}]))")
    toks = await _prom(f"sum by (direction) (increase(lif_tokens_total[{window}]))")
    dec = await _prom(f"sum by (provider) (increase(lif_decisions_total[{window}]))")
    jev_cost = sum((await _prom(f"sum(increase(lif_jev_cost_usd_total[{window}]))")).values())
    avoided = sum((await _prom(f"sum(increase(lif_external_cost_avoided_usd_total[{window}]))")).values())
    reqs = sum((await _prom(f"sum(increase(lif_requests_total[{window}])) - sum(increase(lif_tasks_total{{workload='availability-probe'}}[{window}]) or vector(0))")).values())
    total_tasks = sum(tiers.values())
    heavy = tiers.get("large_local", 0) + tiers.get("external", 0)
    c = config.get("cost") or {}
    local_cost = float(c.get("cpu_tier_incremental_watts", 25)) / 1000 * hours * float(c.get("electricity_usd_per_kwh", 0.16))
    return {
        "window": window, "measured": {"requests": round(reqs), "tasks_by_tier": {k: round(v) for k, v in tiers.items()},
                                       "decisions_by_provider": {k: round(v) for k, v in dec.items()},
                                       "local_tokens": {k: round(v) for k, v in toks.items()},
                                       "external_calls": round(tiers.get("external", 0))},
        "kpi": {"heavy_model_avoidance": round(1 - heavy / total_tasks, 4) if total_tasks else None,
                "llm_avoidance": round((tiers.get("deterministic", 0) + tiers.get("jev", 0)) / total_tasks, 4)
                if total_tasks else None},
        "estimates_usd": {"api_equivalent_value": round(avoided, 4), "jev_spend": round(jev_cost, 6),
                          "local_power_cost": round(local_cost, 4),
                          "net_savings": round(avoided - jev_cost - local_cost, 4),
                          "basis": "ESTIMATE: tokens × cost.external_equivalent_usd_per_mtok; power = "
                                   "cost.cpu_tier_incremental_watts × hours × cost.electricity_usd_per_kwh"},
        "availability_target": config.get("availability_target"),
    }


@app.get("/v1/gpu")
async def gpu():
    return S.gpu.current().to_dict()


# ── models ───────────────────────────────────────────────────────────────────

@app.get("/v1/models")
async def models(state: str | None = None, category: str | None = None):
    rows = S.reg.list(state.split(",") if state else None, category)
    for r in rows:
        b = S.reg.latest_benchmark(r["id"])
        r["last_benchmark"] = {"ts": b["ts"], "summary": b["summary"]} if b else None
        r["aliases"] = S.reg.aliases_using(r["id"])
    return {"models": rows}


@app.get("/v1/models/{mid}")
async def model(mid: str):
    m = S.reg.get(mid)
    if not m:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {**m, "benchmarks": S.reg.benchmarks(mid, 10), "aliases": S.reg.aliases_using(mid),
            "activity": [a for a in S.reg.activity(500) if a["subject"] == mid][:50]}


@app.get("/v1/aliases")
async def aliases():
    return S.reg.aliases()


@app.post("/v1/models/refresh")
async def refresh(request: Request):
    body = await _body(request)
    return _ok(S.life._spawn("discovery", S.disc.run(body.get("categories"), actor=request.state.who)))


@app.get("/v1/discovery/runs")
async def discovery_runs():
    return {"runs": S.reg.discovery_runs(), "running": S.disc.running}


@app.post("/v1/models/{mid}/download")
async def download(mid: str):
    return await _op(S.life.download, mid, "operator")


@app.post("/v1/models/{mid}/benchmark")
async def benchmark(mid: str, request: Request):
    return await _op(S.life.benchmark, mid, "operator", (await _body(request)).get("suite"))


@app.post("/v1/models/nominate")
async def nominate(request: Request):
    """Register one pinned GGUF as a CANDIDATE (e.g. a local/web model): discovery's gates, then the usual
    download → benchmark → promote path."""
    b = await _body(request)
    if not isinstance(b.get("hf_repo"), str) or not isinstance(b.get("file"), str):
        return JSONResponse({"error": "hf_repo and file are required"}, status_code=400)
    return await _op(S.life.nominate, b, "operator")


@app.post("/v1/models/{mid}/load")
async def load(mid: str, request: Request):
    force = (await _body(request)).get("force")
    return await _op(S.life.load, mid, "operator-force" if force else "operator")


@app.post("/v1/models/{mid}/unload")
async def unload(mid: str, request: Request):
    force = (await _body(request)).get("force")
    return await _op(S.life.unload, mid, "operator-force" if force else "operator")


@app.post("/v1/models/{mid}/canary")
async def canary(mid: str, request: Request):
    b = await _body(request)
    return await _op(S.life.canary, mid, "operator", b.get("alias"), b.get("percent"))


@app.post("/v1/models/{mid}/promote")
async def promote(mid: str, request: Request):
    return await _op(S.life.promote, mid, "operator", (await _body(request)).get("alias"))


@app.post("/v1/models/{mid}/{flag}")
async def flag(mid: str, flag: str):
    mapping = {"pin": ("pinned", True), "unpin": ("pinned", False), "block": ("blocked", True),
               "unblock": ("blocked", False)}
    if flag not in mapping:
        return JSONResponse({"error": "unknown action"}, status_code=404)
    return await _op(S.reg.set_flag, mid, *mapping[flag], "operator")


@app.delete("/v1/models/{mid}")
async def delete(mid: str):
    return await _op(S.reg.delete, mid, "operator")


@app.post("/v1/aliases/rollback")
async def rollback(request: Request):
    b = await _body(request)
    if not isinstance(b.get("alias"), str) or not b["alias"].strip():
        return JSONResponse({"error": "alias is required"}, status_code=400)
    return await _op(S.life.rollback, b["alias"].strip(), "operator", b.get("to_version"))


@app.get("/v1/benchmarks")
async def benchmarks(model: str | None = None):
    return {"benchmarks": S.reg.benchmarks(model, 100)}


@app.get("/v1/storage")
async def storage():
    return S.life.storage()


@app.get("/v1/activity")
async def activity(limit: int = 200, since: int = 0):
    return {"activity": S.reg.activity(min(limit, 2000), since)}


@app.get("/v1/availability")
async def availability(window_sec: int = 86400):
    return S.reg.availability(window_sec)


# ── operator controls ────────────────────────────────────────────────────────

SETTINGS = {"discovery_disabled": False, "automatic_discovery": None, "automatic_download": None,
            "automatic_promotion": None, "jev_disabled": False, "maintenance": False, "batch_paused": False,
            "reserve_gpu_mib": 0}


def _settings() -> dict:
    out = {}
    for k, d in SETTINGS.items():
        default = config.get(f"models.{k}") if d is None else d
        out[k] = S.reg.setting(k, default)
    return out


@app.get("/v1/settings")
async def get_settings():
    return _settings()


@app.post("/v1/settings")
async def set_settings(request: Request):
    b = await _body(request)
    bad = [k for k in b if k not in SETTINGS]
    if bad:
        return JSONResponse({"error": f"unknown settings {bad}"}, status_code=400)
    for k, v in b.items():
        S.reg.set_setting(k, v, actor=request.state.who)
    # propagate the switches other services own
    if "jev_disabled" in b:
        await _post(f"{S.decision_url}/decision/control", {"jev_enabled": not b["jev_disabled"]})
    if "batch_paused" in b or "maintenance" in b:
        paused = bool(b.get("batch_paused", False) or b.get("maintenance", False))
        await _post(f"{S.batch_url}/v1/batch/control", {"paused": paused, "reason": "operator"})
    return _settings()


async def _post(url: str, body: dict) -> None:
    try:
        r = await S.http.post(url, json=body, timeout=5,
                              headers={"X-LIF-Internal": config.secret("LIF_INTERNAL_KEY") or ""})
        r.raise_for_status()
    except Exception as e:
        LOG.warning("control propagation failed", extra={"fields": {"url": url, "err": str(e)[:120]}})


class BadBody(Exception):
    pass


@app.exception_handler(BadBody)
async def _bad_body(request: Request, e: BadBody):
    return JSONResponse({"error": str(e)}, status_code=400)


async def _body(request: Request) -> dict:
    """{} for an empty body; malformed or non-object JSON is a 400, never a silent empty update."""
    if not await request.body():
        return {}
    try:
        b = await request.json()
    except ValueError as e:
        raise BadBody("body must be JSON") from e
    if not isinstance(b, dict):
        raise BadBody("body must be a JSON object")
    return b
