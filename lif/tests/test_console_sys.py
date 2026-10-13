"""Console SYS layer: humanize tables, upstream client, poller rollup/notifications, /api/system and /api/models.

Upstreams are faked with httpx.MockTransport using the shapes the real services return (controller,
gateway, Prometheus). Numbers are invented for the tests, not measurements. The app under test is a
minimal FastAPI with only the SYS routers + the shared error handlers, so other owners' middleware
can't change these results.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lif.console import auth, errors, events, poller, upstream
from lif.console import humanize as hz
from lif.console.contracts import ModelRole, User
from lif.console.routes import models as models_routes
from lif.console.routes import system as system_routes

P4B, P17B, PEMB = "qwen3-4b-instruct-2507-q4km-cpu", "qwen3-1.7b-q8-cpu", "qwen3-embedding-0.6b-q8-cpu"
CAND = "acme-coder-7b-q4km-cpu"
NOW = time.time()


def _profile(pid: str, host: str, params_b: float, budget: int) -> dict[str, Any]:
    return {"category": "general", "runtime": "llamacpp", "device": "cpu", "hf_repo": f"Org/{pid}", "revision": "abc123",
            "precision": "q4_k_m", "params_b": params_b, "context": 8192, "memory_budget_mb": budget,
            "endpoint": f"http://{host}.ai-serving.svc:8080"}


def _base() -> dict[str, Any]:
    gpu = {"ts": NOW, "reachable": True, "production_live": False, "production_leases": 0, "background_leases": 0,
           "p_next_hour": 0.05, "forecast_authoritative": True, "admissible_mib": 2048.0, "mem_available_mib": 40000.0,
           "gpu_util_percent": 12.0, "holds": 0, "residents_loaded": {"llm": True, "image": True, "embedding": True},
           "state": "LOW", "reason": "P(production within 1h) = 5%", "age_sec": 2.0}
    caps = {"aliases": {
        "local/auto": {"available": True, "served_by": "decision: request-route", "fallback": False, "degraded": False, "reason": ""},
        "local/fast": {"available": True, "served_by": P4B, "fallback": False, "degraded": False, "reason": ""},
        "local/default": {"available": True, "served_by": P4B, "fallback": False, "degraded": False, "reason": ""},
        "local/instant": {"available": True, "served_by": P17B, "fallback": False, "degraded": False, "reason": ""},
        "local/code": {"available": True, "served_by": P4B, "fallback": False, "degraded": True,
                       "reason": "local/code expects >= 14B; largest resident model is 4B (GPU reserved for the primary workload)"},
        "local/embedding": {"available": True, "served_by": PEMB, "fallback": False, "degraded": False, "reason": ""},
        "local/vision": {"available": False, "reason": "no local model is deployed for local/vision (capacity; see /v1/capabilities)"},
    }, "profiles": {
        P4B: {"endpoint_healthy": True, "params_b": 4, "device": "cpu", "model": "Org/qwen3-4b@abc123", "last_error": ""},
        P17B: {"endpoint_healthy": True, "params_b": 1.7, "device": "cpu", "model": "Org/qwen3-1.7b@def456", "last_error": ""},
        PEMB: {"endpoint_healthy": True, "params_b": 0.6, "device": "cpu", "model": "Org/qwen3-emb@aaa", "last_error": ""},
    }}
    routing = {"profiles": {P4B: _profile(P4B, "tier0", 4, 3584), P17B: _profile(P17B, "tier0-small", 1.7, 2560),
                            PEMB: _profile(PEMB, "embedding", 0.6, 1536)},
               "aliases": {"local/instant": [P17B, P4B], "local/fast": [P4B, P17B], "local/default": [P4B, P17B],
                           "local/reasoning": [P4B], "local/code": [P4B], "local/batch": [P4B, P17B],
                           "local/embedding": [PEMB], "local/vision": [], "local/rerank": []},
               "canaries": {}, "jev_enabled": True,
               "alias_min_params_b": {"local/default": 4.0, "local/reasoning": 30.0, "local/code": 14.0}, "generated": NOW}
    settings = {"discovery_disabled": False, "automatic_discovery": False, "automatic_download": False,
                "automatic_promotion": False, "jev_disabled": False, "maintenance": False, "batch_paused": False,
                "reserve_gpu_mib": 0}
    overview = {"blerbz": gpu, "capabilities": caps,
                "decision_fabric": {"provider": "typesafe_jev", "jev_enabled": True, "jev_breaker_open": False},
                "batch": {"pending": 0, "running": 1, "paused": False, "paused_reason": "", "jobs": {"running": 1, "queued": 2},
                          "recent": [{"id": "bj_aaa", "state": "running", "description": "Summarize notes", "finished": None}]},
                "settings": settings, "tasks": {}, "memory_guard": {"shed": [], "pending": {}},
                "models": {"PRODUCTION": 3, "CANDIDATE": 1}, "controller_uptime_sec": 100}

    def row(pid: str, state: str, **kw: Any) -> dict[str, Any]:
        r = {"id": pid, "model_id": f"Org/{pid}", "revision": "abc123", "category": "general", "state": state,
             "meta": {"params_b": 4}, "profile": routing["profiles"].get(pid, {"device": "cpu", "runtime": "llamacpp"}),
             "fit": {}, "screening": {}, "reason": "", "pinned": False, "blocked": False, "created": NOW - 900,
             "updated": NOW - 600, "last_benchmark": None, "aliases": []}
        r.update(kw)
        return r

    bench = {"ts": NOW - 3000, "summary": {"quality": 0.70, "ttft_ms_p50": 900, "decode_tps_p50": 20.0, "errors": 0, "items": 15}}
    models = {
        P4B: row(P4B, "PRODUCTION", aliases=["local/fast", "local/default", "local/code"], last_benchmark=bench),
        P17B: row(P17B, "PRODUCTION", aliases=["local/instant"]),
        PEMB: row(PEMB, "PRODUCTION", category="embedding", aliases=["local/embedding"]),
        CAND: row(CAND, "APPROVED", meta={"params_b": 7, "model_id": f"Org/{CAND}"},
                  fit={"verdict": "fits_cpu", "total_mib": 5120, "est_cpu_decode_tps": 11.5},
                  profile={"device": "cpu", "runtime": "llamacpp", "precision": "q4_k_m"},
                  screening={"comparison": {"recommendation": "CANARY", "jev": {"improves": "yes", "confidence": 0.81}}},
                  last_benchmark={"ts": NOW - 500, "summary": {"quality": 0.80, "ttft_ms_p50": 1200,
                                                               "decode_tps_p50": 14.0, "errors": 1, "items": 15}}),
    }
    activity = [{"seq": 3, "ts": NOW - 100, "kind": "benchmark_completed", "subject": CAND, "actor": "operator",
                 "detail": {"quality": 0.8, "ttft_ms_p50": 1200, "decode_tps_p50": 14}},
                {"seq": 2, "ts": NOW - 200, "kind": "state_changed", "subject": CAND, "actor": "controller",
                 "detail": {"frm": "CANDIDATE", "to": "DISCOVERED"}},
                {"seq": 1, "ts": NOW - 300, "kind": "alias_changed", "subject": "local/default", "actor": "operator",
                 "detail": {"version": 1, "chain": [P17B]}}]
    runs = {"runs": [{"id": 4, "ts": NOW - 4000, "finished": NOW - 3990, "status": "succeeded", "error": "",
                      "funnel": {"categories": {"coding": {"listed": 1500, "after_listing_filter": 300, "after_deterministic": 120,
                                                           "after_screening": 9, "jev_calls": 18,
                                                           "shortlisted": [f"Org/{CAND}", "Org/other-3b"]}},
                                 "total_ms": 5000}},
                     {"id": 3, "ts": NOW - 9000, "finished": None, "status": "running", "funnel": {}, "error": ""}],
            "running": None}
    aliases = {"local/default": {"alias": "local/default", "version": 2, "chain": [P4B, P17B], "canary": None, "ts": NOW},
               "local/fast": {"alias": "local/fast", "version": 1, "chain": [P4B, P17B], "canary": None, "ts": NOW}}
    return {"gpu": gpu, "caps": caps, "routing": routing, "overview": overview, "settings": settings, "models": models,
            "activity": activity, "runs": runs, "aliases": aliases, "human": {"queue": []},
            "health": {"status": "ok", "gateway": "ready", "useful_local_ai": True, "routing_table": "controller"},
            "prom": {"max(node_memory_MemTotal_bytes)": 120 * 2 ** 30, "max(node_memory_MemAvailable_bytes)": 40 * 2 ** 30,
                     "max(gpusched_capacity_residents_mib)": 50 * 1024, "max(gpusched_capacity_leases_mib)": 0}}


class Fake:
    """In-memory controller + gateway + Prometheus. `down` = hosts that refuse connections; `garbage` = every JSON
    body replaced with an unexpected shape."""

    def __init__(self) -> None:
        self.s = _base()
        self.down: set[str] = set()
        self.garbage: Any = None
        self.calls: list[tuple[str, str, str, dict[str, Any] | None, dict[str, str]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path, method = request.url.host.split(".")[0], request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        self.calls.append((host, method, path, body, dict(request.headers)))
        if host in self.down:
            raise httpx.ConnectError("refused", request=request)
        out = self.route(host, method, path, request.url.params, body)
        if isinstance(out, httpx.Response):
            return out
        if self.garbage is not None:
            return httpx.Response(200, json=self.garbage)
        return httpx.Response(200, json=out)

    def route(self, host: str, method: str, path: str, params: Any, body: Any) -> Any:
        s = self.s
        if host == "gateway":
            return {"/v1/health": s["health"], "/v1/capabilities": s["caps"]}.get(path, httpx.Response(404, json={}))
        if host == "monitoring-kube-prometheus-prometheus":
            q = params.get("query", "")
            if path.endswith("query_range"):
                start, end, step = float(params["start"]), float(params["end"]), float(params["step"])
                ts = [start + i * step for i in range(int((end - start) // step) + 1)]
                vals = {"gpusched_leases": "0.5", "inflight": "0.2", "lif_tasks_total": "40", "batch_items": "0"}
                v = next((x for k, x in vals.items() if k in q), None)
                res = [] if v is None else [{"metric": {}, "values": [[t, v] for t in ts]}]
                return {"status": "success", "data": {"resultType": "matrix", "result": res}}
            if q in s["prom"]:
                return {"status": "success", "data": {"result": [{"metric": {}, "value": [NOW, str(s["prom"][q])]}]}}
            if q.startswith("ALERTS{"):
                return {"status": "success", "data": {"result": s.get("alerts", [])}}
            return {"status": "success", "data": {"result": []}}
        # controller
        if path == "/v1/gpu":
            return s["gpu"]
        if path == "/v1/overview":
            return s["overview"]
        if path == "/v1/activity":
            since = int(params.get("since", 0))
            return {"activity": [a for a in s["activity"] if not isinstance(a.get("seq"), int) or a["seq"] > since]}
        if path == "/v1/routing":
            return s["routing"]
        if path == "/v1/de/human":
            return s["human"]
        if path == "/v1/settings":
            if method == "POST":
                s["settings"].update(body)
            return s["settings"]
        if path == "/v1/discovery/runs":
            return s["runs"]
        if path == "/v1/models/refresh":
            return {"status": "started", "task": "discovery"}
        if path == "/v1/aliases":
            return s["aliases"]
        if path == "/v1/aliases/rollback":
            return {"alias": body["alias"], "version": 3, "note": "rollback to v1"}
        if path == "/v1/storage":
            return {"production": {"count": 3, "bytes": 0, "models": [P4B]}}
        if path == "/v1/models":
            states = set((params.get("state") or "").split(",")) - {""}
            return {"models": [m for m in s["models"].values() if not states or m["state"] in states]}
        if path.startswith("/v1/models/"):
            parts = path.split("/")[3:]
            mid = parts[0]
            if mid not in s["models"]:
                return httpx.Response(409 if len(parts) > 1 else 404, json={"error": f"unknown model {mid}"})
            if method == "DELETE":
                del s["models"][mid]
                return {"ok": True}
            if len(parts) == 1:
                m = s["models"][mid]
                lb = m.get("last_benchmark")
                return {**m, "benchmarks": [{"id": 1, "model": mid, "suite": "core", **lb}] if lb else [], "activity": []}
            return {"status": "started", "task": f"{parts[1]}:{mid}"}
        return httpx.Response(404, json={"error": "not found"})


ADMIN = User(id="u1", name="owner", role="admin", perms=list(auth.ALL_PERMS))
DEVICE = User(id="u2", name="phone", role="device", perms=sorted(auth.ROLE_PERMS["device"]))


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Fake:
    monkeypatch.setenv("LIF_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("LIF_CONSOLE_ADMIN_KEY", "test-admin-key")
    monkeypatch.setenv("LIF_CONSOLE_GATEWAY_KEY", "test-gateway-key")
    monkeypatch.setenv("LIF_CONSOLE_DB", str(tmp_path / "console.db"))
    monkeypatch.delenv("LIF_KNOWLEDGE_URL", raising=False)
    for k in ("LIF_CONTROLLER_URL", "LIF_GATEWAY_URL", "LIF_BATCH_URL", "LIF_PROMETHEUS_URL"):
        monkeypatch.delenv(k, raising=False)
    f = Fake()
    upstream.set_transport(httpx.MockTransport(f))
    poller.reset()
    models_routes._disc.update(requested_at=0.0, known_max=0, categories=[])
    yield f
    upstream.set_transport(None)
    poller.reset()


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    monkeypatch.setattr(events.hub, "publish", lambda t, d, audience=None: out.append((t, d)))
    return out


def make_client(user: User) -> TestClient:
    app = FastAPI()
    errors.install(app)
    app.include_router(system_routes.router)
    app.include_router(models_routes.router)
    app.dependency_overrides[auth.current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def refresh() -> poller.Snapshot:
    return asyncio.run(poller.refresh())


# ── humanize ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("term,health,text", [
    ("CrashLoopBackOff", "offline", "Service repeatedly failed to start"),
    ("ImagePullBackOff", "offline", "Couldn't download the service image"),
    ("Pending", "busy", "Waiting for resources"),
    ("OOMKilled", "degraded", "Ran out of memory and restarted"),
    ("ContainerCreating", "busy", "Starting up"),
    ("SomethingNew", "unknown", "In an unusual state"),
])
def test_k8s_terms_are_translated(term: str, health: str, text: str) -> None:
    assert hz.k8s_state(term) == (health, text)


def test_model_states_cover_all_lifecycle_states() -> None:
    for s in ("DISCOVERED", "CANDIDATE", "DOWNLOADING", "STAGED", "VALIDATING", "BENCHMARKING", "APPROVED", "CANARY",
              "PRODUCTION", "STANDBY", "DEPRECATED", "QUARANTINED", "REJECTED", "FAILED"):
        label, _ = hz.model_state(s)
        assert label and label != "Unknown state" and s not in label
    assert hz.model_state("PRODUCTION") == ("In use", "healthy")


def test_blerbz_states_told_apart_by_fields() -> None:
    base = _base()["gpu"]
    assert hz.blerbz(base)[0] == "idle"
    busy = hz.blerbz({**base, "state": "IMMINENT", "production_live": True})
    assert busy[0] == "busy" and busy[1] == "Running" and busy[2] == "GPU reserved for Primary workload."
    assert hz.blerbz({**base, "state": "IMMINENT", "residents_loaded": {"llm": False}})[1] == "Reloading"
    assert hz.blerbz({**base, "state": "IMMINENT", "age_sec": 90})[0] == "unknown"           # stale = blind
    assert hz.blerbz({**base, "state": "IMMINENT", "reachable": False, "reason": "gpusched unreachable (fail-safe)"})[0] == "unknown"
    assert hz.blerbz({**base, "state": "HIGH"})[0] == "imminent"
    assert hz.blerbz({**base, "state": "IMMINENT", "reason": "other"})[0] == "reserved"
    assert hz.blerbz({})[0] == "unknown"


@pytest.mark.parametrize("reason,kw,cause", [
    ("primary unavailable: All connection attempts failed", {"fallback": True, "degraded": False}, "primary_unavailable"),
    ("primary unavailable: All connection attempts failed", {"fallback": True, "degraded": False, "shed": True},
     "shed_by_memory_guard"),
    ("primary unavailable: GPU engine reserved for primary-workload production", {"fallback": True, "degraded": False},
     "yielded_to_primary"),
    ("latency budget during primary-workload production", {"fallback": True, "degraded": False}, "yielded_to_primary"),
    ("local/code expects >= 14B; largest resident model is 4B (GPU reserved for the primary workload)",
     {"fallback": False, "degraded": True}, "below_quality_floor"),
    ("canary (10% of local/default)", {"fallback": False, "degraded": False}, "canary"),
    ("no local model is deployed for local/vision (capacity; see /v1/capabilities)", {"fallback": False, "degraded": False},
     "not_deployed"),
    ("all models for local/fast are unavailable: x: y", {"fallback": False, "degraded": False}, "primary_unavailable"),
    ("upstream_error on qwen: boom", {"fallback": True, "degraded": False}, "primary_unavailable"),
    ("", {"fallback": False, "degraded": False}, None),
])
def test_role_cause_parses_router_reasons(reason: str, kw: dict[str, Any], cause: str | None) -> None:
    got, label = hz.role_cause(reason, **kw)
    assert got == cause
    assert (label is None) == (cause is None)
    if label:
        assert "primary unavailable" not in label and "expects >=" not in label     # raw text never leaks
    if cause == "below_quality_floor":
        assert "4B instead of 14B" in label


def test_job_states() -> None:
    assert hz.job_state("queued", "waiting: primary workload IMMINENT (1 production lease(s) live): batch/eval paused") == \
        ("waiting", "Waiting for Primary workload", True)
    assert hz.job_state("queued", "waiting: batch paused by operator (x)")[0] == "paused"
    assert hz.job_state("failed: boom", None) == ("failed", "Failed", False)
    assert hz.job_state("done", None)[0] == "completed"
    assert hz.job_state("expired", "deadline passed")[0] == "failed"
    assert hz.job_state("running", None) == ("running", "Running", False)


def test_activity_translation_and_noise() -> None:
    rows = _base()["activity"]
    ev = hz.activity(rows[0])
    assert ev and ev.category == "model" and ev.severity == "success" and "Benchmark finished" in ev.title
    assert ev.detail and "Quality 80%" in ev.detail
    assert ev.href == f"/models/deployments/{CAND}"
    assert hz.activity(rows[1]) is None             # CANDIDATE↔DISCOVERED flapping is noise
    shed = hz.activity({"seq": 9, "ts": 1, "kind": "memory_guard_shed", "subject": "embedding", "actor": "gpu-resource-manager",
                        "detail": {"mem_available_mib": 6000}})
    assert shed and shed.title == "Paused LIF embedding service to free memory" and shed.severity == "warning"
    assert hz.activity({"seq": 10, "ts": 1, "kind": "decision_human", "subject": "human/1"}).category == "decision"
    assert hz.activity({"seq": 11, "ts": 1, "kind": "brand_new_kind"}).title == "Brand new kind"
    assert hz.activity({"seq": 12, "kind": "setting_changed", "subject": "maintenance", "detail": {"value": True}}).title \
        == "Maintenance mode turned on"


def test_model_names_and_decision_routes() -> None:
    assert hz.model_name(P4B) == "Qwen3 4B Instruct 2507 (CPU)"
    assert hz.model_name("Qwen/Qwen3-1.7B-GGUF@abc") == "Qwen3 1.7B"
    assert hz.decision_route("auto") == "Decided automatically (high confidence)"


def test_system_health_rollup() -> None:
    ok = {"status": "ok", "useful_local_ai": True}
    common = dict(controller_ok=True, gpu=_base()["gpu"], mem_available_mib=40000.0, shed=[], roles=[], alerts=[],
                  maintenance=False, batch_paused=False, decision={})
    assert hz.system_health(gateway_health=ok, **common)[:2] == ("healthy", "Labzilla is healthy")
    fb = [ModelRole(role="balanced", fallback_active=True, cause="primary_unavailable")]
    assert hz.system_health(gateway_health=ok, **{**common, "roles": fb})[:2] == ("degraded", "Running on fallback model")
    assert hz.system_health(gateway_health=None, **{**common, "roles": fb})[0] == "offline"
    low = hz.system_health(gateway_health=ok, **{**common, "mem_available_mib": 5000.0})
    assert low[0] == "degraded" and "safety margin" in low[1]
    alert = [{"metric": {"alertname": "Watchdog", "severity": "none"}}]
    assert hz.system_health(gateway_health=ok, **{**common, "alerts": alert})[0] == "healthy"


# ── upstream ─────────────────────────────────────────────────────────────────────────────────

async def test_upstream_auth_headers_and_errors(fake: Fake) -> None:
    await upstream.get("controller", "/v1/gpu")
    await upstream.get("gateway", "/v1/capabilities")
    ctrl, gw = fake.calls[0][4], fake.calls[1][4]
    assert ctrl["authorization"] == "Bearer test-admin-key" and "x-lif-workload" not in ctrl
    assert gw["authorization"] == "Bearer test-gateway-key" and gw["x-lif-workload"] == "console"
    with pytest.raises(upstream.UpstreamError) as e:
        await upstream.post("controller", "/v1/models/nope/benchmark", {})
    assert e.value.status == 409 and "unknown model nope" in e.value.detail and "test-admin-key" not in str(e.value)
    h = upstream.to_human(e.value, doing="start the benchmark")
    assert h.status == 409 and h.error.title == "Couldn't start the benchmark" and "unknown model nope" in h.error.impact
    fake.down.add("controller")
    with pytest.raises(upstream.UpstreamError) as e2:
        await upstream.get("controller", "/v1/gpu")
    assert e2.value.unreachable and upstream.to_human(e2.value).error.title == "Model control service isn't answering"
    assert upstream.to_human(upstream.UpstreamError("gateway", 401, "bad key")).error.title.startswith(
        "The console isn't connected")


async def test_upstream_non_json_and_prom_fallback(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    upstream.set_transport(httpx.MockTransport(lambda r: httpx.Response(200, text="<html>bad gateway</html>")))
    with pytest.raises(upstream.UpstreamError) as e:
        await upstream.get("controller", "/v1/gpu")
    assert e.value.status == 502
    upstream.set_transport(httpx.MockTransport(fake))
    fake.down.add("monitoring-kube-prometheus-prometheus")
    assert await upstream.prom("up") == []
    assert await upstream.prom_range("up", 0, 10, 5) == []
    with pytest.raises(upstream.UpstreamError):
        await upstream.prom("up", strict=True)


# ── poller ───────────────────────────────────────────────────────────────────────────────────

def test_snapshot_before_first_cycle_is_honest(fake: Fake) -> None:
    s = poller.snapshot()
    assert s.status.health == "unknown" and s.status.headline == "Checking Labzilla…"


def test_poller_builds_healthy_status(fake: Fake, published: list[tuple[str, Any]]) -> None:
    snap = refresh()
    st = snap.status
    assert st.health == "healthy" and st.headline == "Labzilla is healthy"
    assert st.local_ai == "healthy" and st.local_ai_label == "Local AI Ready"
    assert st.primary_model == "Qwen3 4B Instruct 2507 (CPU)"
    assert st.resource.mem_total_gb == 120.0 and st.resource.mem_available_gb == 40.0 and st.resource.blerbz == "idle"
    assert st.jobs.running == 1 and st.jobs.queued == 2
    by = {r.role: r for r in snap.roles}
    assert by["fast"].state_label == "Ready" and by["balanced"].alias == "local/default"
    assert by["code"].state == "degraded" and by["code"].cause == "below_quality_floor"
    assert by["vision"].state_label == "Not installed" and by["vision"].cause == "not_deployed"
    assert by["auto"].state == "healthy"
    assert [e.id for e in snap.activity] == ["act:3", "act:1"]           # flapping row dropped
    assert {s.key for s in snap.services} >= {"gateway", "controller", "decision", "batch", "prometheus",
                                              f"model:{P4B}"}
    assert "gpusched" not in {s.key for s in snap.services}          # scheduler is optional and off
    assert st.resource.primary_enabled is False
    assert [t for t, _ in published].count("status") == 1 and not [t for t, _ in published if t == "notification"]
    assert "blerbz" not in {w.key for w in snap.compute.work}
    assert {w.key for w in snap.compute.work} >= {"ai_serving", "available"} or snap.compute.note


def test_fallback_raises_one_notification_and_memory_guard_cause(fake: Fake, published: list[tuple[str, Any]]) -> None:
    refresh()
    caps = fake.s["overview"]["capabilities"]
    caps["aliases"]["local/default"] = {"available": True, "served_by": P17B, "fallback": True, "degraded": True,
                                        "reason": "primary unavailable: All connection attempts failed"}
    caps["aliases"]["local/instant"] = {"available": True, "served_by": P4B, "fallback": True, "degraded": False,
                                        "reason": "primary unavailable: All connection attempts failed"}
    fake.s["overview"]["memory_guard"]["shed"] = ["tier0-small"]
    snap = refresh()
    by = {r.role: r for r in snap.roles}
    assert snap.status.headline == "Running on fallback model" and snap.status.health == "degraded"
    assert by["balanced"].cause == "primary_unavailable" and by["balanced"].fallback_active
    assert by["instant"].cause == "shed_by_memory_guard"           # tier0-small is local/instant's primary
    kinds = [d.kind for t, d in published if t == "notification"]
    assert kinds.count("fallback") == 1 and kinds.count("memory_pressure") == 1
    assert any(t == "model" and d.role == "balanced" for t, d in published)
    published.clear()
    refresh()                                                       # still on fallback: no repeat
    assert not [d for t, d in published if t == "notification"]
    assert any(n.kind == "fallback" for n in poller.snapshot().status.notifications)
    caps["aliases"]["local/default"] = copy.deepcopy(_base()["caps"]["aliases"]["local/default"])
    refresh()
    assert not any(n.kind == "fallback" for n in poller.snapshot().status.notifications)


def test_canary_pick_is_not_reported_as_change(fake: Fake, published: list[tuple[str, Any]]) -> None:
    fake.s["routing"]["canaries"] = {"local/fast": {"profile": CAND, "percent": 10, "since": NOW}}
    refresh()
    caps = fake.s["overview"]["capabilities"]
    caps["aliases"]["local/fast"] = {"available": True, "served_by": CAND, "fallback": True, "degraded": False,
                                     "reason": "canary (10% of local/fast)"}
    published.clear()
    snap = refresh()
    fast = next(r for r in snap.roles if r.role == "fast")
    assert fast.served_by == P4B and not fast.fallback_active and fast.cause == "canary" and fast.canary.percent == 10
    assert not [d for t, d in published if t in ("model", "notification")]


def test_registry_change_publishes_a_model_event(fake: Fake, published: list[tuple[str, Any]]) -> None:
    """A finished check adds a candidate without touching any role: Models must still hear about it."""
    refresh()
    refresh()
    published.clear()
    refresh()
    assert not [d for t, d in published if t == "model"]                 # nothing changed, nothing said
    fake.s["models"]["vision-cand"] = {**fake.s["models"][CAND], "id": "vision-cand", "category": "vision"}
    poller._st.src["models"].attempt_ts = 0.0                              # due now (30 s cadence)
    refresh()
    assert [d.summary for t, d in published if t == "model"] == ["Model registry changed"]


def test_new_activity_publishes_and_candidate_notifies(fake: Fake, published: list[tuple[str, Any]]) -> None:
    refresh()
    fake.s["activity"].insert(0, {"seq": 4, "ts": NOW, "kind": "comparison_complete", "subject": CAND, "actor": "controller",
                                  "detail": {"recommendation": "CANARY"}})
    snap = refresh()
    assert snap.activity[0].id == "act:4"
    assert [d.id for t, d in published if t == "activity"] == ["act:4"]
    note = [d for t, d in published if t == "notification"]
    assert len(note) == 1 and note[0].kind == "candidate" and note[0].href == f"/models/candidates/{CAND}"


def test_blerbz_busy_and_controller_down(fake: Fake, published: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lif.gpu.workload.enabled", lambda: True)
    monkeypatch.setattr("lif.gpu.workload.display_name", lambda: "Primary workload")
    fake.s["gpu"].update(state="IMMINENT", production_live=True, production_leases=1)
    fake.s["overview"]["batch"]["pending"] = 3
    snap = refresh()
    assert snap.status.health == "busy" and snap.status.resource.blerbz_reason == "GPU reserved for Primary workload."
    assert snap.status.resource.primary_enabled is True
    assert any(n.kind == "blerbz_contention" for n in snap.status.notifications)
    assert "BLERBZ" not in (snap.status.headline + snap.status.resource.blerbz_reason)
    fake.down.add("controller")
    snap = refresh()
    ctrl = next(s for s in snap.services if s.key == "controller")
    assert ctrl.health == "offline" and snap.status.health == "attention"
    assert snap.roles                                               # last-good routing/caps still shown
    assert any(d.kind == "service_down" for t, d in published if t == "notification")


# ── routes ───────────────────────────────────────────────────────────────────────────────────

GETS = ["/api/system/status", "/api/system/compute", "/api/system/services", "/api/system/timeline?hours=6",
        "/api/system/storage", "/api/system/logs?level=all", "/api/system/alerts", "/api/system/settings",
        "/api/models", "/api/models/roles/fast", f"/api/models/deployments/{P4B}", "/api/models/discovery",
        "/api/models/candidates", f"/api/models/candidates/{CAND}/compare",
        f"/api/models/deployments/{CAND}/preview?action=promote", "/api/models/roles/balanced/rollback/preview"]


def test_all_reads_ok(fake: Fake, published: list[tuple[str, Any]]) -> None:
    refresh()
    c = make_client(ADMIN)
    for path in GETS:
        r = c.get(path)
        assert r.status_code == 200, (path, r.text)


@pytest.mark.parametrize("mode", ["down", "garbage_list", "garbage_dict", "garbage_str"])
def test_reads_never_500_when_upstreams_misbehave(fake: Fake, published: list[tuple[str, Any]], mode: str) -> None:
    refresh()                       # populate the cache first, then break everything
    if mode == "down":
        fake.down |= {"controller", "gateway", "monitoring-kube-prometheus-prometheus", "batch"}
    else:
        fake.garbage = {"garbage_list": [1, 2], "garbage_dict": {"error": "x", "models": "nope"}, "garbage_str": "x"}[mode]
    refresh()
    c = make_client(ADMIN)
    for path in GETS:
        r = c.get(path)
        assert r.status_code < 500 or r.status_code in (502, 503), (mode, path, r.status_code, r.text)
        if r.status_code >= 400:
            assert r.json()["error"]["title"], (path, r.text)
    if mode == "down":
        assert c.get("/api/system/timeline").json()["available"] is False
        assert c.get("/api/system/alerts").json()["available"] is False
        assert c.get("/api/system/settings").json()["available"] is False
        assert c.get("/api/models").json()["roles"]


def test_status_from_empty_cache_with_everything_down(fake: Fake) -> None:
    fake.down |= {"controller", "gateway", "monitoring-kube-prometheus-prometheus"}
    snap = refresh()
    assert snap.status.health == "offline" and snap.status.local_ai == "offline"
    r = make_client(ADMIN).get("/api/models")
    assert r.status_code == 200 and r.json()["deployments"] == []


def test_device_cannot_release_or_change_settings(fake: Fake, published: list[tuple[str, Any]]) -> None:
    refresh()
    c = make_client(DEVICE)
    for path, body in [(f"/api/models/deployments/{CAND}/promote", {}),
                       (f"/api/models/deployments/{P17B}/delete", {"confirm": P17B}),
                       (f"/api/models/deployments/{CAND}/load", {}),
                       ("/api/models/roles/balanced/rollback", {"confirm": "local/default"}),
                       ("/api/system/settings", {"key": "maintenance", "value": True})]:
        r = c.post(path, json=body)
        assert r.status_code == 403 and r.json()["error"]["title"] == "Not allowed from this session", path
    r = c.post("/api/system/settings", json={"key": "batch_paused", "value": True})
    assert r.status_code == 200, r.text
    assert c.post("/api/models/discovery", json={}).status_code == 200
    assert c.post("/api/system/services/gateway/retry").status_code == 200


def test_typed_confirmation_for_delete(fake: Fake, published: list[tuple[str, Any]]) -> None:
    fake.s["models"]["old-model"] = {**fake.s["models"][CAND], "id": "old-model", "state": "REJECTED", "aliases": []}
    c = make_client(ADMIN)
    pv = c.get("/api/models/deployments/old-model/preview?action=delete").json()
    assert pv["confirm"] == "typed" and pv["confirm_text"] == "old-model" and "can't be undone" in pv["rollback"]
    r = c.post("/api/models/deployments/old-model/delete", json={"confirm": "old"})
    assert r.status_code == 422 and "Type old-model" in r.json()["error"]["next_step"]
    assert not any(m == "DELETE" for _, m, *_ in fake.calls)
    r = c.post("/api/models/deployments/old-model/delete", json={"confirm": "old-model"})
    assert r.status_code == 200 and any(m == "DELETE" and p == "/v1/models/old-model" for _, m, p, *_ in fake.calls)
    # a model in use can't be deleted: refused before reaching the controller
    r = c.post(f"/api/models/deployments/{P4B}/delete", json={"confirm": P4B})
    assert r.status_code == 409 and "in use" in r.json()["error"]["impact"]


def test_operation_prevalidation(fake: Fake, published: list[tuple[str, Any]]) -> None:
    c = make_client(ADMIN)
    r = c.post(f"/api/models/deployments/{CAND}/canary", json={"percent": 150})
    assert r.status_code == 422 and r.json()["error"]["title"].startswith("The trial share")
    r = c.post(f"/api/models/deployments/{CAND}/promote", json={"alias": "local/made-up"})
    assert r.status_code == 422 and r.json()["error"]["title"] == "That isn't a model role"
    r = c.post("/api/models/deployments/ghost/pin")
    assert r.status_code == 404 and r.json()["error"]["title"] == "Model not found"
    assert not any(p == "/v1/models/ghost/pin" for _, _, p, *_ in fake.calls)
    r = c.post(f"/api/models/deployments/{CAND}/test")
    assert r.status_code == 409 and "benchmark" in r.json()["error"]["next_step"].lower()
    r = c.post(f"/api/models/deployments/{CAND}/promote", json={})
    assert r.status_code == 200, r.text
    sent = next(b for h, m, p, b, _ in fake.calls if p == f"/v1/models/{CAND}/promote")
    assert sent == {"alias": "local/default"}       # category default, validated
    pv = c.get(f"/api/models/deployments/{CAND}/preview?action=promote").json()
    assert any("changes from Qwen3 4B Instruct 2507 (CPU) to Acme Coder 7B (CPU)" in x for x in pv["changes"])
    assert "rollback" in pv["rollback"] or "roll" in pv["rollback"]
    r = c.post(f"/api/models/deployments/{P4B}/unload")
    assert r.status_code == 409                    # roles depend on it


def test_rollback_requires_alias_confirmation(fake: Fake, published: list[tuple[str, Any]]) -> None:
    refresh()
    c = make_client(ADMIN)
    pv = c.get("/api/models/roles/balanced/rollback/preview").json()
    assert pv["confirm"] == "typed" and pv["confirm_text"] == "local/default"
    assert any("to Qwen3 1.7B (CPU)" in x for x in pv["changes"])      # reconstructed from alias_changed history
    assert c.post("/api/models/roles/balanced/rollback", json={"confirm": "balanced"}).status_code == 422
    r = c.post("/api/models/roles/balanced/rollback", json={"confirm": "local/default"})
    assert r.status_code == 200 and any(p == "/v1/aliases/rollback" for _, _, p, *_ in fake.calls)
    assert c.post("/api/models/roles/fast/rollback", json={"confirm": "local/fast"}).status_code == 409   # version 1


def test_settings_keep_maintenance_pause(fake: Fake, published: list[tuple[str, Any]]) -> None:
    fake.s["settings"]["maintenance"] = True
    c = make_client(ADMIN)
    r = c.post("/api/system/settings", json={"key": "batch_paused", "value": False})
    assert r.status_code == 200
    sent = next(b for h, m, p, b, _ in fake.calls if p == "/v1/settings" and m == "POST")
    assert sent == {"batch_paused": False, "maintenance": True}
    r = c.post("/api/system/settings", json={"key": "reserve_gpu_mib", "value": -5})
    assert r.status_code == 422
    r = c.post("/api/system/settings", json={"key": "reserve_gpu_mib", "value": 1.5})    # never truncated to 1
    assert r.status_code == 422
    r = c.post("/api/system/settings", json={"key": "maintenance", "value": 3})
    assert r.status_code == 422
    items = {i["key"]: i for i in c.get("/api/system/settings").json()["items"]}
    assert items["batch_paused"]["perm"] == "jobs.control" and items["maintenance"]["value"] is True


def test_discovery_runs_and_start(fake: Fake, published: list[tuple[str, Any]]) -> None:
    refresh()
    c = make_client(ADMIN)
    st = c.get("/api/models/discovery").json()
    latest = st["recent"][0]
    assert [s["label"] for s in latest["stages"]] == ["Discovering (1,500 models found)", "Filtering (120 relevant)",
                                                      "Evaluating (9 candidates)", "Benchmarking (2 shortlisted for testing)"]
    assert latest["upgrades"] == 1 and latest["summary"] == "1 meaningful upgrade found"   # CAND passed with CANARY
    fake.s["models"][CAND]["screening"] = {}
    refresh()
    assert "worth testing" in c.get("/api/models/discovery").json()["recent"][0]["summary"]
    assert st["recent"][1]["status"] == "interrupted"
    assert c.post("/api/models/discovery", json={"categories": ["poetry"]}).status_code == 422
    r = c.post("/api/models/discovery", json={"categories": ["coding"]})
    assert r.status_code == 200 and r.json()["current"]["status"] == "running"
    assert all(s["count"] is None for s in r.json()["current"]["stages"])     # no invented progress
    assert any(p == "/v1/models/refresh" and b == {"categories": ["coding"]} for _, _, p, b, _ in fake.calls)
    r = c.post("/api/models/discovery", json={"categories": ["vision"]})      # vision models can be searched for
    assert r.status_code == 200, r.text
    fake.s["settings"]["discovery_disabled"] = True
    refresh()
    r = c.post("/api/models/discovery", json={})
    assert r.status_code == 409 and r.json()["error"]["title"] == "Model checks are turned off"


def test_candidate_comparison(fake: Fake, published: list[tuple[str, Any]]) -> None:
    refresh()
    c = make_client(ADMIN)
    cmp_ = c.get(f"/api/models/candidates/{CAND}/compare").json()
    rows = {r["metric"]: r for r in cmp_["rows"]}
    assert rows["Quality"]["current"] == 70.0 and rows["Quality"]["candidate"] == 80.0 and rows["Quality"]["verdict"] == "better"
    assert rows["Time to first token"]["verdict"] == "worse" and rows["Throughput"]["verdict"] == "worse"
    assert rows["Memory"]["note"]                                    # candidate memory is an estimate
    assert cmp_["recommendation_code"] == "canary" and cmp_["advisory"].startswith("Jev advice (not a measurement)")
    assert cmp_["incumbent"]["id"] == P4B
    fake.s["models"][CAND]["last_benchmark"] = None
    fake.s["models"][CAND]["screening"] = {}
    cmp2 = c.get(f"/api/models/candidates/{CAND}/compare").json()
    assert cmp2["recommendation_code"] == "benchmark_first"
    thr = {r["metric"]: r for r in cmp2["rows"]}["Throughput"]
    assert thr["candidate"] == 11.5 and "estimated" in thr["note"]
    items = c.get("/api/models/candidates").json()
    assert items[0]["deployment"]["id"] == CAND and items[0]["role"] == "balanced"


def test_timeline_storage_alerts_logs(fake: Fake, published: list[tuple[str, Any]]) -> None:
    c = make_client(ADMIN)
    tl = c.get("/api/system/timeline?hours=3").json()
    assert tl["available"] and len(tl["buckets"]) == 3
    assert tl["buckets"][-1]["label"] == "GPU + inference" and tl["buckets"][-1]["blerbz_pct"] == 50.0
    assert tl["primary_enabled"] is False and "BLERBZ" not in tl["buckets"][-1]["label"]
    fake.s["alerts"] = [{"metric": {"alertname": "LIFHostHeadroomLow", "severity": "warning", "alertstate": "firing"}},
                        {"metric": {"alertname": "Watchdog", "severity": "none", "alertstate": "firing"}}]
    al = c.get("/api/system/alerts").json()
    assert [a["summary"] for a in al["alerts"]] == ["Memory is below the safety margin"]
    sto = c.get("/api/system/storage").json()
    assert sto["items"][0]["label"] == "Models in use" and sto["items"][0]["used_gb"] is None
    logs = c.get("/api/system/logs?level=all&q=benchmark").json()
    assert [e["id"] for e in logs["events"]] == ["act:3"] and logs["raw_logs_available"] is False
    assert c.get("/api/system/logs?level=error").json()["events"] == []
    assert c.get("/api/system/logs?level=bogus").status_code == 422


# ── first deployment, restarts, nested garbage ───────────────────────────────────────────────

def test_keys_not_rolled_out_is_setup_not_outage(fake: Fake, published: list[tuple[str, Any]]) -> None:
    """Before the owner rolls the services, only /v1/routing and gateway /v1/health answer; everything else is 401."""
    orig = fake.route

    def route(host: str, method: str, path: str, params: Any, body: Any) -> Any:
        if (host == "controller" and path != "/v1/routing") or (host == "gateway" and path != "/v1/health"):
            return httpx.Response(401, json={"error": {"message": "invalid key", "type": "auth"}})
        return orig(host, method, path, params, body)

    fake.route = route  # type: ignore[method-assign]
    snap = refresh()
    assert snap.status.health == "attention"
    assert snap.status.headline.startswith("The console isn't connected to the model control service yet")
    assert "rejected" in snap.status.headline
    ctrl = next(s for s in snap.services if s.key == "controller")
    assert ctrl.health == "attention" and "access key" in ctrl.summary
    fast = next(r for r in snap.roles if r.role == "fast")
    assert fast.state_label == "Unknown" and fast.chain            # routing is open, live status isn't
    assert not any(t == "notification" and d.kind == "service_down" for t, d in published)


def test_missing_gateway_key_is_reported(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIF_CONSOLE_GATEWAY_KEY")
    snap = refresh()
    assert any("AI gateway yet (its access key isn't configured)" in t.value for t in snap.status.tech)
    assert not any(h == "gateway" and p == "/v1/capabilities" for h, _, p, *_ in fake.calls)


def test_restart_does_not_reannounce_existing_conditions(fake: Fake, published: list[tuple[str, Any]]) -> None:
    fake.s["overview"]["capabilities"]["aliases"]["local/default"] = {
        "available": True, "served_by": P17B, "fallback": True, "degraded": True,
        "reason": "primary unavailable: All connection attempts failed"}
    orig = fake.route
    overview_down = {"on": True}

    def route(host: str, method: str, path: str, params: Any, body: Any) -> Any:
        if path == "/v1/overview" and overview_down["on"]:
            return httpx.Response(502, text="<html>bad gateway</html>")
        return orig(host, method, path, params, body)

    fake.route = route  # type: ignore[method-assign]
    refresh()                                   # cycle 1: overview missing → not seeded yet
    overview_down["on"] = False
    refresh()                                   # cycle 2: fallback was already true at start → not announced
    assert not [d for t, d in published if t == "notification"]
    assert any(n.kind == "fallback" for n in poller.snapshot().status.notifications)
    fake.s["overview"]["memory_guard"]["shed"] = ["embedding"]      # a real transition after seeding
    refresh()
    assert [d.kind for t, d in published if t == "notification"] == ["memory_pressure"]


@pytest.mark.parametrize("patch", [
    {"overview": {"batch": 7, "tasks": "x", "memory_guard": [1], "settings": None, "capabilities": {"aliases": 3}}},
    {"routing": {"aliases": 5, "profiles": [1], "canaries": "x"}},
    {"runs": {"runs": 9, "running": "x"}},
    {"gpu": {"state": 3, "residents_loaded": "x", "age_sec": "old"}},
    {"activity": [{"seq": "x", "kind": 5, "detail": "y"}, {"seq": 99, "kind": "state_changed", "detail": [1]}]},
])
def test_nested_wrong_types_never_500(fake: Fake, published: list[tuple[str, Any]], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(fake.s[key], dict) and isinstance(value, dict):
            fake.s[key].update(value)
        else:
            fake.s[key] = value
    refresh()
    c = make_client(ADMIN)
    for path in GETS:
        r = c.get(path)
        assert r.status_code < 500 or r.status_code in (502, 503), (patch, path, r.text)
    assert c.post("/api/system/services/gateway/retry").status_code < 500


def test_cached_live_state_expires_when_upstreams_go_quiet(fake: Fake, published: list[tuple[str, Any]],
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lif.gpu.workload.enabled", lambda: True)
    fake.s["human"] = {"queue": [{"id": 7, "status": "pending", "ts": NOW, "decision_ref": "model-advance/v1",
                                  "package": {"labels": ["yes", "no"], "reason": "low_confidence"}}]}
    snap = refresh()
    assert snap.status.approvals_pending == 1 and any(r.state_label == "Ready" for r in snap.roles)
    fake.down |= {"controller", "gateway", "monitoring-kube-prometheus-prometheus", "batch"}
    for s in poller._st.src.values():           # the last good answers are now 15 minutes old
        s.ok_ts -= 900
    snap = refresh()
    assert not [r for r in snap.roles if r.state_label == "Ready"]
    by = {s.key: s for s in snap.services}
    assert by["gpusched"].health != "healthy" and by[f"model:{P4B}"].summary != "Answering"
    assert snap.status.approvals_pending == 0
    assert not [n for n in snap.status.notifications if n.kind == "approval"]


def test_jev_no_is_not_an_endorsement(fake: Fake, published: list[tuple[str, Any]]) -> None:
    assert [hz.jev_improves(v) for v in ("yes", "no", "NO", True, False, None, "maybe")] == \
        [True, False, False, True, False, None, None]
    fake.s["models"][CAND]["screening"]["comparison"]["jev"] = {"improves": "no", "confidence": 0.7}
    refresh()
    adv = make_client(ADMIN).get(f"/api/models/candidates/{CAND}/compare").json()["advisory"]
    assert "probably not an improvement" in adv


def test_timeline_ignores_the_controllers_probes() -> None:
    # controller/app.py sends X-LIF-Workload: availability-probe; the gateway uses it as the workload label
    probe = re.search(r'"X-LIF-Workload":\s*"([^"]+)"', (Path(system_routes.__file__).parents[2] / "controller" /
                                                           "app.py").read_text()).group(1)
    q = system_routes.TIMELINE_QUERIES["tasks"]
    assert probe == "availability-probe" and re.search(r'workload!~"[^"]*\b' + re.escape(probe), q)


def test_job_counts_keep_their_units(fake: Fake, published: list[tuple[str, Any]]) -> None:
    fake.s["overview"]["tasks"] = {"download:m1": "failed: disk full", "benchmark:m2": "running"}
    fake.s["overview"]["batch"].update(running=2, pending=2875, jobs={"running": 1, "queued": 1})
    snap = refresh()
    assert snap.status.jobs.failed_24h == 0               # an untimed task failure isn't "in the last 24 h"
    batch = next(s for s in snap.services if s.key == "batch")
    assert batch.summary == "2 items running, 2,875 waiting"
    from lif.console import intent
    act = intent.resolve("Pause batch jobs", snap, ADMIN).proposed_action
    assert act is not None and act.impact.startswith("2 batch jobs will wait")     # not the benchmark task


def test_activity_bursts_and_empty_start_are_not_lost(fake: Fake, published: list[tuple[str, Any]]) -> None:
    fake.s["activity"] = []
    refresh()                                   # a fresh controller: nothing yet
    calls = []
    orig = fake.route

    def newest_first(host: str, method: str, path: str, params: Any, body: Any) -> Any:
        if path == "/v1/activity":              # the registry's real semantics: newest `limit` rows after `since`
            since, limit = int(params.get("since", 0)), int(params.get("limit", 200))
            calls.append(limit)
            rows = [a for a in fake.s["activity"] if a["seq"] > since]
            return {"activity": sorted(rows, key=lambda a: -a["seq"])[:limit]}
        return orig(host, method, path, params, body)
    fake.route = newest_first                   # type: ignore[method-assign]
    fake.s["activity"] = [{"seq": 1, "ts": NOW, "kind": "comparison_complete", "subject": CAND,
                           "actor": "controller", "detail": {"recommendation": "CANARY"}}]
    refresh()
    assert any(t == "notification" and d.kind == "candidate" for t, d in published)    # first real batch announced
    fake.s["activity"] += [{"seq": i, "ts": NOW, "kind": "model_registered", "subject": f"m{i}", "actor": "controller",
                            "detail": {}} for i in range(2, 253)]
    refresh()
    assert calls[-1] == 2000 and set(range(3, 53)) <= set(poller._st.rows) and poller._st.max_seq == 252


def test_resent_pause_flag_is_not_announced(fake: Fake, published: list[tuple[str, Any]]) -> None:
    refresh()
    fake.s["settings"]["maintenance"] = True
    c = make_client(ADMIN)
    assert c.post("/api/system/settings", json={"key": "batch_paused", "value": False}).status_code == 200
    top = max(a["seq"] for a in fake.s["activity"] if isinstance(a.get("seq"), int))
    fake.s["activity"][:0] = [  # what the controller logs for {"batch_paused": false, "maintenance": true}
        {"seq": top + 2, "ts": time.time(), "kind": "setting_changed", "subject": "maintenance", "actor": "owner",
         "detail": {"value": True}},
        {"seq": top + 1, "ts": time.time(), "kind": "setting_changed", "subject": "batch_paused", "actor": "owner",
         "detail": {"value": False}}]
    snap = refresh()
    titles = [e.title for e in snap.activity]
    assert not [t for t in titles if t.startswith("Maintenance mode turned on")]
    assert [t for t in titles if t.startswith("Batch pause")]
    snap = refresh()                            # the next poll doesn't fetch the skipped row again
    assert not [e for e in snap.activity if e.title.startswith("Maintenance mode turned on")]


def test_in_use_models_offer_no_unload_and_maintenance_needs_no_confirm(fake: Fake, published: list[tuple[str, Any]]) -> None:
    assert "unload" not in poller.model_actions("PRODUCTION", "cpu", False, False, used=True)
    assert "unload" in poller.model_actions("PRODUCTION", "cpu", False, False, used=False)
    from lif.console import intent
    snap = refresh()
    on = intent.resolve("Turn on maintenance mode", snap, ADMIN).proposed_action
    assert on is not None and on.confirm == "none"          # same as the Settings switch (§41)


def test_refusal_previews_are_recognisable(fake: Fake, published: list[tuple[str, Any]]) -> None:
    """The UI treats confirm "none" without a rollback line as a refusal and never POSTs it (no surprise 409)."""
    refresh()
    c = make_client(ADMIN)
    refused = [c.get(f"/api/models/deployments/{P4B}/preview?action=unload").json(),     # a role uses it
               c.get("/api/models/roles/fast/rollback/preview").json()]                   # first version
    for pv in refused:
        assert pv["confirm"] == "none" and not pv["rollback"] and pv["changes"], pv
    assert refused[1]["title"] == "Nothing to roll back"
    runnable = c.get(f"/api/models/deployments/{P4B}/preview?action=load").json()
    assert runnable["confirm"] != "none" or runnable["rollback"]


def test_run_error_drops_status_codes() -> None:
    out = hz.run_error("HTTP 503: upstream unavailable")
    assert "503" not in out and "HTTP" not in out and "upstream unavailable" in out
    assert hz.run_error(None) == "The run reported an error."
    assert hz.run_error("", "") == "" and len(hz.run_error("x" * 500)) == 200
