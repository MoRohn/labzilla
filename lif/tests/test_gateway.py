"""Gateway end-to-end against a fake llama.cpp upstream (no cluster, no Jev, no gpusched)."""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from lif.gpu.state import BlerbzState, Snapshot

KEY = "test-key-123"
seen: list[dict] = []
broken: set[str] = set()


def upstream(req: httpx.Request) -> httpx.Response:
    host = req.url.host
    if req.url.path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    if host.split(".")[0] in broken:
        return httpx.Response(500, json={"error": "boom"})
    body = json.loads(req.content)
    seen.append({"host": host, **body})
    if req.url.path == "/v1/embeddings":
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}], "usage": {"prompt_tokens": 2}})
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hi"},
                                                  "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": 5, "completion_tokens": 1},
                                     "timings": {"predicted_per_second": 20.0}})


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("LIF_GATEWAY_KEYS", f"tester:{KEY}")
    monkeypatch.delenv("LIF_CONTROLLER_URL", raising=False)
    from lif.gateway import app as gw
    with TestClient(gw.app) as c:
        # Stop the gateway's background loops (gpusched watcher, prober, table refresh) on their
        # own event loop first: an in-flight real gpusched poll would otherwise land after the
        # fixture and fail safe to IMMINENT mid-test.
        for t in gw.S.tasks:
            t.get_loop().call_soon_threadsafe(t.cancel)
        __import__("time").sleep(0.2)
        gw.S.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        gw.S.router.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        for n in gw.S.router.health:
            gw.S.router.health[n].ok = True
        gw.S.gpu._snap = Snapshot(ts=__import__("time").time(), reachable=True, state=BlerbzState.LOW, reason="test")
        seen.clear()
        broken.clear()
        yield c, gw


H = {"Authorization": f"Bearer {KEY}"}


def test_auth_required(client):
    c, _ = client
    assert c.post("/v1/chat/completions", json={}).status_code == 401
    assert c.get("/v1/health").status_code == 200


def test_logical_alias_and_metadata(client):
    c, _ = client
    r = c.post("/v1/chat/completions", headers=H, json={"model": "local/default", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    d = r.json()
    assert d["model"] == "local/default"                      # caller never sees physical ids in `model`
    assert d["lif"]["served_by"] == "qwen3-4b-instruct-2507-q4km-cpu" and d["lif"]["fallback"] is False
    assert seen[-1]["host"].startswith("tier0.")


def test_fallback_on_upstream_error_is_visible(client):
    c, _ = client
    broken.add("tier0")
    r = c.post("/v1/chat/completions", headers=H, json={"model": "local/default", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    lif = r.json()["lif"]
    assert lif["served_by"] == "qwen3-1.7b-q8-cpu" and lif["fallback"] and "upstream_error" in lif["reason"]
    assert lif["degraded"]                                     # 1.7B < local/default's 4B floor


def test_blerbz_imminent_clamps_tokens(client, monkeypatch):
    monkeypatch.setattr("lif.gpu.workload.enabled", lambda: True)
    c, gw = client
    gw.S.gpu._snap = Snapshot(ts=__import__("time").time(), reachable=True, production_live=True,
                              state=BlerbzState.IMMINENT, reason="production lease live")
    r = c.post("/v1/chat/completions", headers=H, json={"model": "local/fast", "max_tokens": 4000,
                                                        "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200 and seen[-1]["max_tokens"] == 512 and r.json()["lif"]["blerbz"] == "IMMINENT"


def test_no_model_for_alias_is_503_with_reason(client):
    c, _ = client
    r = c.post("/v1/chat/completions", headers=H, json={"model": "local/vision", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 503 and "no local model" in r.json()["error"]["message"]


def test_deterministic_cache(client):
    c, _ = client
    body = {"model": "local/fast", "temperature": 0, "messages": [{"role": "user", "content": "same"}]}
    c.post("/v1/chat/completions", headers=H, json=body)
    n = len(seen)
    r = c.post("/v1/chat/completions", headers=H, json=body)
    assert len(seen) == n and r.json()["lif"]["cache"] == "hit"


def test_auto_alias_uses_decision(client):
    c, _ = client
    r = c.post("/v1/chat/completions", headers=H, json={"model": "local/auto", "messages": [
        {"role": "user", "content": "Refactor this python function: def f(x): return x"}]})
    lif = r.json()["lif"]
    assert lif["requested"] == "local/auto" and lif["route_decision"]["decision"] == "code"
    assert lif["route_decision"]["provider"] == "rules"          # private prompt → never sent to Jev
    assert lif["fallback"] is False                              # auto choosing an alias is routing, not a fallback
    assert r.headers.get("x-lif-fallback") in (None, "false")


def test_embeddings_and_models(client):
    c, _ = client
    assert c.post("/v1/embeddings", headers=H, json={"model": "local/embedding", "input": "x"}).status_code == 200
    ids = {m["id"] for m in c.get("/v1/models", headers=H).json()["data"]}
    assert {"local/fast", "local/default", "local/embedding", "local/auto"} <= ids


async def test_yield_cap_is_cluster_wide():
    """Two gateway replicas, one upstream: while IMMINENT, a request waits until the model
    server itself reports fewer in-flight requests than the cap (1)."""
    from lif.gateway.app import YieldLimiter
    upstream_inflight = [1]                      # the OTHER replica is already using the slot
    a = YieldLimiter()
    async def busy():
        return upstream_inflight[0]
    async def release_later():
        await asyncio.sleep(0.6)
        upstream_inflight[0] = 0
    t0 = time.perf_counter()
    asyncio.get_running_loop().create_task(release_later())
    async with a.slot("tier0", lambda: 1, timeout=5, upstream_busy=busy):
        waited = time.perf_counter() - t0
    assert waited >= 0.5                         # did not run concurrently with the other replica
    upstream_inflight[0] = 1
    with pytest.raises(TimeoutError):
        async with a.slot("tier0", lambda: 1, timeout=0.5, upstream_busy=busy):
            pass


def test_engine_key_sent_to_shared_gpu_profile(client, monkeypatch):
    c, gw = client
    from lif.routing.router import Profile
    gw.S.router.profiles["gpu32"] = Profile(name="gpu32", endpoint="http://bridge.test:18102", category="general",
                                            params_b=32, device="gpu-shared", auth_secret="LIF_ENGINE_KEY",
                                            yield_on_blerbz=True)
    gw.S.router.health["gpu32"] = gw.S.router.health["qwen3-4b-instruct-2507-q4km-cpu"].__class__(ok=True)
    gw.S.router.aliases["local/reasoning"] = ["gpu32", "qwen3-4b-instruct-2507-q4km-cpu"]
    monkeypatch.setenv("LIF_ENGINE_KEY", "engine-secret")
    auth_seen = []
    orig_post = gw.S.http.post
    async def post(url, json=None, headers=None, **kw):
        auth_seen.append((url, (headers or {}).get("Authorization")))
        return await orig_post(url, json=json, headers=headers, **kw)
    monkeypatch.setattr(gw.S.http, "post", post)
    r = c.post("/v1/chat/completions", headers=H, json={"model": "local/reasoning", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200 and r.json()["lif"]["served_by"] == "gpu32", r.json()["lif"]
    assert auth_seen[-1] == ("http://bridge.test:18102/v1/chat/completions", "Bearer engine-secret")


def test_return_progress_only_reaches_llama_cpp(client):
    """The console asks for llama-server's prompt-progress chunks; another runtime may reject the field."""
    _, gw = client
    from types import SimpleNamespace
    from lif.gpu.state import BlerbzState
    from lif.routing.router import Profile
    body = {"model": "local/fast", "stream": True, "return_progress": True, "messages": []}
    for runtime, kept in (("llama.cpp", True), ("vllm", False)):
        prof = Profile.from_cfg("p", {"endpoint": "http://x", "runtime": runtime})
        out = gw._prepare(body, SimpleNamespace(profile=prof), BlerbzState.LOW)
        assert ("return_progress" in out) is kept, runtime
    assert Profile.from_cfg("p", {"endpoint": "http://x"}).runtime == "llama.cpp"


# ── vision: images route deterministically, never to a text-only model ──────

IMG = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
                                   {"type": "text", "text": "What colour is this?"}]}


def _add_vision(gw, healthy=True):
    from lif.routing.router import Health, Profile
    gw.S.router.profiles["vl"] = Profile.from_cfg("vl", {"endpoint": "http://vl.test:8080", "category": "vision",
                                                         "params_b": 4, "mmproj": {"file": "mmproj-Q8_0.gguf"}})
    gw.S.router.health["vl"] = Health(ok=healthy)
    gw.S.router.aliases["local/vision"] = ["vl"]


def test_auto_with_image_goes_to_vision_without_route_decision(client, monkeypatch):
    c, gw = client
    _add_vision(gw)
    calls = []
    orig = gw.S.fabric.evaluate

    async def spy(name, *a, **kw):
        calls.append(name)
        return await orig(name, *a, **kw)
    monkeypatch.setattr(gw.S.fabric, "evaluate", spy)
    r = c.post("/v1/chat/completions", headers=H, json={"model": "local/auto", "messages": [IMG]})
    assert r.status_code == 200, r.json()
    lif = r.json()["lif"]
    assert lif["served_by"] == "vl" and lif["alias"] == "local/vision" and lif["requested"] == "local/auto"
    assert lif["fallback"] is False                                    # the live smoke showed true before the fix
    assert lif["route_decision"]["decision"] == "vision" and lif["route_decision"]["provider"] == "rules"
    assert "request-route" not in calls                                # never through the Jev route decision
    assert seen[-1]["host"] == "vl.test" and seen[-1]["messages"][0]["content"][0]["type"] == "image_url"
    assert gw.S.router.alias_status()["local/vision"]["vision"] is True
    assert c.get("/v1/capabilities", headers=H).json()["profiles"]["vl"]["vision"] is True


def test_image_to_text_alias_is_422_not_supported(client):
    c, _ = client
    r = c.post("/v1/chat/completions", headers=H, json={"model": "local/default", "messages": [IMG]})
    assert r.status_code == 422
    e = r.json()["error"]
    assert e["type"] == "not_supported" and e["code"] == "images_not_supported"
    assert "local/vision" in e["message"] and e["lif"]["use"] == "local/vision"
    assert not seen                                                   # nothing reached an upstream


def test_image_without_vision_model_is_503_with_clear_reason(client):
    c, _ = client
    for model in ("local/vision", "local/auto"):
        r = c.post("/v1/chat/completions", headers=H, json={"model": model, "messages": [IMG]})
        assert r.status_code == 503
        msg = r.json()["error"]["message"]
        assert msg.startswith("no local model is deployed for local/vision") and "no vision model is installed yet" in msg


def test_unhealthy_vision_never_falls_back_to_text(client):
    c, gw = client
    _add_vision(gw, healthy=False)
    gw.S.router.aliases["local/vision"] = ["vl", "qwen3-4b-instruct-2507-q4km-cpu"]   # a mis-built chain
    r = c.post("/v1/chat/completions", headers=H, json={"model": "local/vision", "messages": [IMG]})
    assert r.status_code == 503 and not seen
    # text-only requests to that alias may still use the text model in its chain
    r = c.post("/v1/chat/completions", headers=H, json={"model": "local/vision",
                                                        "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200 and r.json()["lif"]["served_by"] == "qwen3-4b-instruct-2507-q4km-cpu"


def test_latency_budget_never_reroutes_images(client, monkeypatch):
    monkeypatch.setattr("lif.gpu.workload.enabled", lambda: True)
    c, gw = client
    _add_vision(gw)
    gw.S.gpu._snap = Snapshot(ts=time.time(), reachable=True, production_live=True,
                              state=BlerbzState.IMMINENT, reason="production lease live")
    r = c.post("/v1/chat/completions", headers=H, json={"model": "local/vision", "messages": [IMG],
                                                        "lif": {"budget": {"max_latency_ms": 500}}})
    assert r.status_code == 200 and r.json()["lif"]["served_by"] == "vl"
