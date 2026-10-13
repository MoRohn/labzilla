"""Console AI/work backend: command-bar intents, Ask streaming, jobs, agents/approvals, knowledge filtering.

No network: the gateway is an httpx.MockTransport on routes/ai.py, every other upstream goes through
upstream.set_transport(), the poller snapshot is a fixture, and the console database is a temp file.
Knowledge filtering runs against the real public workspace (lif/knowledge) and a tmp workspace that
contains a CONFIDENTIAL repo and a CONFIDENTIAL object inside a PUBLIC repo.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import textwrap
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lif.console import auth, db, errors, events, intent, poller, threads, upstream
from lif.console.contracts import (ActivityEvent, Approval, ComputeView, JobsSummary, ModelRole, Notification,
                                   ResourceState, ServiceHealth, SystemStatus, User, WorkShare)
from lif.console.routes import agents, ai, jobs, knowledge

LIF = Path(__file__).resolve().parents[1]
ADMIN = User(id="u_owner", name="owner", role="admin", perms=list(auth.ALL_PERMS))
DEVICE = User(id="u_owner", name="owner", role="device", device_name="Phone",
              perms=sorted(auth.ROLE_PERMS["device"]))


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────

def make_snapshot(**over: Any) -> poller.Snapshot:
    now = time.time()
    roles = [
        ModelRole(role="fast", alias="local/fast", label="Fast", state="healthy", state_label="Ready",
                  model_name="Qwen3 4B Instruct 2507 (CPU)", served_by="qwen3-4b-instruct-2507-q4km-cpu",
                  chain=["Qwen3 4B Instruct 2507 (CPU)", "Qwen3 1.7B (CPU)"]),
        ModelRole(role="balanced", alias="local/default", label="Balanced", state="degraded", state_label="Degraded",
                  model_name="Qwen3 1.7B (CPU)", served_by="qwen3-1.7b-q8-cpu", fallback_active=True,
                  cause="yielded_to_primary", cause_label="Main model paused while Primary workload is running; using a backup",
                  chain=["Qwen3 4B Instruct 2507 (CPU)", "Qwen3 1.7B (CPU)"]),
        ModelRole(role="vision", alias="local/vision", label="Vision", state="offline", state_label="Unavailable",
                  cause="not_deployed", cause_label="No model is installed for this role"),
    ]
    status = SystemStatus(
        health="busy", headline="Primary workload is using the GPU", local_ai="healthy", local_ai_label="Local AI Ready",
        primary_model="Qwen3 4B", resource=ResourceState(
            primary_enabled=True, primary_name="Primary workload",
            blerbz="busy", blerbz_label="Running", blerbz_reason="GPU reserved for Primary workload.",
            gpu_util_pct=87.0, mem_total_gb=128.0, mem_used_gb=96.0, mem_available_gb=32.0),
        jobs=JobsSummary(running=1, queued=2, waiting=3, failed_24h=0), approvals_pending=1,
        notifications=[Notification(id="n1", severity="warning", title="Fallback active", body="Balanced is on a backup",
                                    kind="fallback")],
        services=[ServiceHealth(key="batch", name="Batch service", health="offline",
                                summary="Service repeatedly failed to start")])
    snap = poller.Snapshot(
        status=status, roles=roles,
        compute=ComputeView(work=[WorkShare(key="blerbz", label="Primary workload", gb=40.0), WorkShare(key="ai_serving",
                                                                                              label="AI serving", gb=6.0)]),
        activity=[ActivityEvent(id="a1", ts=now - 60, category="model", title="Model refresh completed — 2 better "
                                "candidates found"),
                  ActivityEvent(id="a0", ts=now - 3 * 86400, category="job", title="Old event")],
        services=status.services, approvals=[Approval(id="review:1", title="Review a decision", blocking=False)],
        jobs=status.jobs, raw={"settings": {"maintenance": False, "discovery_disabled": False,
                                            "automatic_discovery": True}, "tasks": {}},
        updated_at=now)
    for k, v in over.items():
        setattr(snap, k, v)
    return snap


class Hub:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any, Any]] = []

    def __call__(self, type: str, data: Any, audience: Any = None) -> None:
        self.events.append((type, data, audience))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LIF_CONSOLE_DB", str(tmp_path / "console.db"))
    monkeypatch.setenv("LIF_CONSOLE_GATEWAY_KEY", "test-gateway-key")
    monkeypatch.setenv("LIF_KNOWLEDGE_ROOT", str(LIF / "knowledge"))
    monkeypatch.delenv("LIF_KNOWLEDGE_URL", raising=False)
    monkeypatch.delenv("LIF_KNOWLEDGE_EMBED_URL", raising=False)
    db.close()
    db.init(tmp_path / "console.db")
    snap = make_snapshot()
    monkeypatch.setattr(poller, "snapshot", lambda: snap)
    hub = Hub()
    monkeypatch.setattr(events.hub, "publish", hub)
    state: dict[str, Any] = {"user": ADMIN, "snap": snap, "hub": hub}
    yield state
    upstream.set_transport(None)
    ai._transport = None
    db.close()


@pytest.fixture()
def client(env):
    app = FastAPI()
    app.state.limiters = auth.Limiters()          # fresh buckets: Ask allows 20 messages a minute
    errors.install(app)
    for r in (ai.router, jobs.router, agents.router, knowledge.router):
        app.include_router(r)
    app.dependency_overrides[auth.current_user] = lambda: env["user"]
    with TestClient(app) as c:
        yield c


def upstreams(routes: dict[tuple[str, str, str], Any], calls: list[httpx.Request] | None = None) -> None:
    """Fake controller/batch/knowledge: {(host prefix, METHOD, path): json | (status, json) | callable}."""
    def handler(req: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(req)
        host = req.url.host.split(".")[0]
        hit = routes.get((host, req.method, req.url.path))
        if hit is None:
            return httpx.Response(404, json={"error": f"no fake for {req.method} {req.url.path}"})
        if callable(hit):
            hit = hit(req)
        status, body = hit if isinstance(hit, tuple) else (200, hit)
        return httpx.Response(status, json=body)
    upstream.set_transport(httpx.MockTransport(handler))


def sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for frame in text.strip().split("\n\n"):
        ev = next((ln[7:] for ln in frame.splitlines() if ln.startswith("event: ")), None)
        data = "\n".join(ln[6:] for ln in frame.splitlines() if ln.startswith("data: "))
        if ev and data:
            out.append((ev, json.loads(data)))
    return out


def chunk(text: str | None = None, **extra: Any) -> str:
    body: dict[str, Any] = {"choices": [{"delta": {"content": text} if text else {}, "finish_reason": None}]}
    body.update(extra)
    return f"data: {json.dumps(body)}\n\n"


def gateway(responses: list[httpx.Response] | httpx.Response, seen: list[httpx.Request]) -> None:
    queue = responses if isinstance(responses, list) else [responses]

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return queue.pop(0) if len(queue) > 1 else queue[0]
    ai._transport = httpx.MockTransport(handler)


HAPPY_HEADERS = {"X-LIF-Served-By": "qwen3-4b-instruct-2507-q4km-cpu", "X-LIF-Fallback": "true",
                 "X-LIF-Degraded": "false", "X-LIF-Requested": "local/auto", "content-type": "text/event-stream"}


def happy_stream(provider: str = "rules", alias: str = "local/fast") -> httpx.Response:
    meta = {"requested": "local/auto", "alias": alias, "served_by": "qwen3-4b-instruct-2507-q4km-cpu",
            "model": "Qwen/Qwen3-4B-Instruct-2507-GGUF@0123456789ab", "fallback": True, "degraded": False,
            "route_decision": {"decision": "fast", "confidence": 0.97, "provider": provider, "action": "auto"}}
    body = chunk("Hello") + chunk(" world") + \
        f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})}\n\n" + \
        f"data: {json.dumps({'choices': [], 'usage': {'prompt_tokens': 12, 'completion_tokens': 2}, 'lif': meta})}\n\n" + \
        "data: [DONE]\n\n"
    return httpx.Response(200, headers=HAPPY_HEADERS, content=body.encode())


def test_ask_stream_reports_waiting_then_reading_progress(client, env):
    """Before the first word the browser hears where the answer is: waiting for a slot, then reading the
    prompt with llama.cpp's own progress (total / cached / processed). Progress after text starts is dropped."""
    seen: list[httpx.Request] = []
    prog = lambda total, cache, done: f"data: {json.dumps({'choices': [], 'prompt_progress': {'total': total, 'cache': cache, 'processed': done, 'time_ms': 1}})}\n\n"
    body = prog(1700, 1200, 1200) + prog(1700, 1200, 1700) + chunk("Hi") + prog(1700, 1200, 1700) + "data: [DONE]\n\n"
    gateway(httpx.Response(200, headers=HAPPY_HEADERS, content=body.encode()), seen)
    t = client.post("/api/ai/threads", json={"mode": "auto", "privacy": "local_only"}).json()
    evs = sse(client.post(f"/api/ai/threads/{t['id']}/messages",
                          json={"content": "hello", "mode": "auto", "privacy": "local_only"}).text)
    phases = [d for e, d in evs if e == "phase"]
    assert [p["phase"] for p in phases] == ["waiting", "reading", "reading", "reading"]
    assert phases[2] == {"phase": "reading", "total": 1700, "cached": 1200, "processed": 1200}
    assert phases[3]["processed"] == 1700
    kinds = [e for e, _ in evs]
    assert kinds.index("phase") < kinds.index("delta") and "phase" not in kinds[kinds.index("delta"):]
    assert json.loads(seen[0].content)["return_progress"] is True


# ── command bar: every spec example (§7, §8, §33, §50, §103) ─────────────────────────────────

INTENTS = [
    # §7
    ("Ask the local model to explain unified memory", "ai_prompt"),
    ("Run the code agent on repo X", "agent_request"),
    ("Check for better coding models", "model_request"),
    ("Why is the GPU busy?", "system_query"),
    ("Pause batch jobs", "operational_command"),
    ("What changed today?", "system_query"),
    ("Summarize current incidents", "system_query"),
    # §8
    ("Why did local/default fall back to local/fast?", "system_query"),
    ("Write a Python parser for this JSON", "ai_prompt"),
    ("Check whether any better coding models were released", "model_request"),
    # §33 knowledge questions
    ("Why are we using K3s?", "knowledge_query"),
    ("What depends on the GPU reserve?", "knowledge_query"),
    ("What happened in the last OOM incident?", "knowledge_query"),
    ("Which decisions changed this week?", "knowledge_query"),
    # §50 palette / navigation
    ("open gpu", "navigation"),
    ("settings", "navigation"),
    ("Open Settings", "navigation"),
    ("open logs", "navigation"),
    # §103 first-time user tasks
    ("Which model handled my last request?", "system_query"),
    ("Why did the AI fall back?", "system_query"),
    ("Check GPU load", "system_query"),
    ("What is BLERBZ using?", "system_query"),
    ("Pause a batch job", "operational_command"),
    ("Check for better models", "model_request"),
    ("Approve an agent action", "system_query"),
    ("Connect a phone", "navigation"),
    # §104 and operations
    ("Review this code snippet", "ai_prompt"),
    ("Resume batch jobs", "operational_command"),
    ("Turn on maintenance mode", "operational_command"),
    ("maintenance off", "operational_command"),
    ("Write a script that pauses batch jobs", "ai_prompt"),
    ("hello there", "ai_prompt"),
]


@pytest.mark.parametrize("text,kind", INTENTS)
def test_intent_table(text, kind):
    got, conf = intent.classify(text)
    assert got == kind, text
    assert 0 < conf <= 1


def test_command_answers_from_snapshot(env):
    snap, u = env["snap"], ADMIN
    gpu = intent.resolve("Why is the GPU busy?", snap, u)
    assert "Primary workload" in gpu.answer and "BLERBZ" not in gpu.answer and gpu.navigate == "/system/compute"
    assert {"GPU load": "87%", "Primary workload": "Running"}.items() <= {f.label: f.value for f in gpu.facts}.items()

    fb = intent.resolve("Why did local/default fall back to local/fast?", snap, u)
    assert "Main model paused while Primary workload is running" in fb.answer and "fallback" in fb.answer
    assert fb.navigate == "/models/roles/balanced"
    assert {f.label: f.value for f in fb.facts}["Fallback order"] == "Qwen3 4B Instruct 2507 (CPU) → Qwen3 1.7B (CPU)"

    changes = intent.resolve("What changed today?", snap, u)
    assert "2 better candidates" in changes.answer and "Old event" not in changes.answer
    # no server wall-clock times (the container runs in UTC, not the owner's zone): relative ones only
    assert "1 min ago: Model refresh" in changes.answer and not re.search(r"\b\d\d:\d\d\b", changes.answer)

    inc = intent.resolve("Summarize current incidents", snap, u)
    assert "Batch service" in inc.answer and "Fallback active" in inc.answer

    jobs_ = intent.resolve("What jobs are queued?", snap, u)
    assert jobs_.kind == "system_query" and "2 queued" in jobs_.answer

    gen = intent.resolve("Write a Python parser for this JSON", snap, u)
    assert gen.prompt and gen.prompt.text == "Write a Python parser for this JSON" and gen.proposed_action is None
    ask = intent.resolve("Ask the local model: what is a monad?", snap, u)
    assert ask.prompt.text == "what is a monad?"
    assert intent.resolve("open gpu", snap, u).navigate == "/system/compute"
    assert intent.resolve("settings", snap, u).navigate == "/system/settings"
    assert intent.resolve("Connect a phone", snap, u).navigate == "/connect"


def test_command_actions_are_proposals(env):
    snap = env["snap"]
    pause = intent.resolve("Pause batch jobs", snap, ADMIN)       # exactly what the palette sends
    a = pause.proposed_action
    assert a.path == "/api/jobs/batch/pause-all" and a.body == {"paused": True} and a.perm == "jobs.control"
    assert a.confirm == "none" and a.impact
    disc = intent.resolve("Check whether any better coding models were released", snap, ADMIN)
    assert disc.proposed_action.path == "/api/models/discovery"
    assert disc.proposed_action.body == {"categories": ["coding"]}
    assert intent.resolve("check for better models", snap, ADMIN).proposed_action.body == {"categories": None}
    maint = intent.resolve("Turn on maintenance mode", snap, ADMIN).proposed_action
    assert maint.body == {"key": "maintenance", "value": True} and maint.perm == "system.settings"
    assert maint.confirm == "none"          # reversible, like the Settings switch (§41)
    # a phone may not change settings: the action is still shown, with an honest note
    dev = intent.resolve("Turn on maintenance mode", snap, DEVICE)
    assert "admin session" in dev.answer
    # no code agent exists: say so and offer the local model instead
    agent = intent.resolve("Run the code agent on repo X", snap, ADMIN)
    assert "doesn't have a code agent" in agent.answer and agent.prompt.mode == "code"
    assert agent.proposed_action is None


def test_command_gpu_states(env):
    snap = env["snap"]
    snap.status.resource = ResourceState(primary_enabled=True, primary_name="Primary workload",
                                         blerbz="imminent", blerbz_label="Likely soon", gpu_util_pct=12.0,
                                         blerbz_reason="Primary workload is likely to start within the hour.")
    soon = intent.resolve("Why is the GPU busy?", snap, ADMIN).answer
    assert soon.startswith("**The GPU isn't busy**") and "likely to start within the hour" in soon
    snap.status.resource = ResourceState(primary_enabled=True, primary_name="Primary workload",
                                         blerbz="unknown", blerbz_label="Unknown",
                                         blerbz_reason="Labzilla can't see the GPU scheduler.")
    assert "can't see the GPU scheduler" in intent.resolve("Check GPU load", snap, ADMIN).answer


def test_command_not_ready_is_honest(env):
    empty = poller.Snapshot()
    for text in ("Why is the GPU busy?", "What changed today?", "Summarize current incidents",
                 "Why did local/default fall back?"):
        assert intent.resolve(text, empty, ADMIN).answer == intent.NOT_READY


def test_command_endpoint_and_last_request(client, env):
    gateway(happy_stream(), [])
    t = client.post("/api/ai/threads", json={"mode": "auto", "privacy": "local_only"}).json()
    client.post(f"/api/ai/threads/{t['id']}/messages", json={"content": "Summarise BLERBZ", "mode": "auto"})
    r = client.post("/api/command", json={"text": "Which model handled my last request?"}).json()
    assert r["kind"] == "system_query" and r["navigate"] == f"/ask/{t['id']}"
    assert "Fast" in r["answer"] and "Qwen3 4B Instruct 2507 (CPU)" in r["answer"]
    k = client.post("/api/command", json={"text": "Why are we using K3s?"}).json()
    assert k["kind"] == "knowledge_query" and "K3s" in k["answer"] and k["facts"]
    assert client.post("/api/command", json={"text": "  "}).status_code == 422


# ── Ask streaming ────────────────────────────────────────────────────────────────────────────

def test_ask_stream_happy_path(client, env):
    seen: list[httpx.Request] = []
    gateway(happy_stream(), seen)
    t = client.post("/api/ai/threads", json={"mode": "auto", "privacy": "local_only"}).json()
    r = client.post(f"/api/ai/threads/{t['id']}/messages",
                    json={"content": "Explain unified memory\nin two lines", "mode": "auto", "privacy": "local_only"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evs = sse(r.text)
    kinds = [e for e, _ in evs]
    assert kinds[0] == "route" and kinds[-1] == "done" and "receipt" in kinds and "error" not in kinds
    assert "".join(d["text"] for e, d in evs if e == "delta") == "Hello world"
    rec = next(d for e, d in evs if e == "receipt")
    assert rec["alias"] == "local/fast" and rec["role_label"] == "Fast"
    assert rec["model_name"] == "Qwen3 4B Instruct 2507 (CPU)"       # catalogue name from the snapshot
    assert [s["label"] for s in rec["route"]] == ["Auto", "Local Fast"]
    assert rec["privacy"] == "local_only" and rec["fallback"] is False   # auto → fast is routing, not a fallback
    assert rec["decision"]["decided_by"] == "rules" and rec["decision"]["confidence"] == 0.97
    assert rec["tokens"] == {"prompt": 12, "completion": 2} and rec["latency_ms"] is not None
    assert "test-gateway-key" not in r.text
    # what went upstream: local/auto, CONFIDENTIAL, workload console, streaming
    req = seen[0]
    assert req.url.path == "/v1/chat/completions"
    assert req.headers["x-lif-data-class"] == "CONFIDENTIAL" and req.headers["x-lif-workload"] == "console"
    body = json.loads(req.content)
    assert body["model"] == "local/auto" and body["stream"] is True
    assert body["messages"][-1] == {"role": "user", "content": "Explain unified memory\nin two lines"}
    # persisted before done, visible from another device of the same owner
    env["user"] = DEVICE
    th = client.get(f"/api/ai/threads/{t['id']}").json()
    assert th["title"] == "Explain unified memory"
    assert [m["role"] for m in th["messages"]] == ["user", "assistant"]
    assert th["messages"][1]["content"] == "Hello world" and th["messages"][1]["status"] == "done"
    assert th["messages"][1]["receipt"]["alias"] == "local/fast"
    listing = client.get("/api/ai/threads").json()
    assert listing[0]["id"] == t["id"] and listing[0]["group"] == "today"
    thread_events = [d for typ, d, aud in env["hub"].events if typ == "thread" and d.kind == "message"]
    assert thread_events[-1].status == "done" and thread_events[-1].content == "Hello world"
    assert all(aud == "user:u_owner" for typ, _, aud in env["hub"].events if typ == "thread")


def test_ask_allow_jev_route_and_context(client, env):
    seen: list[httpx.Request] = []
    gateway([happy_stream(), happy_stream(provider="jev", alias="local/reasoning")], seen)
    t = client.post("/api/ai/threads", json={"mode": "auto", "privacy": "allow_jev"}).json()
    client.post(f"/api/ai/threads/{t['id']}/messages", json={"content": "first", "privacy": "allow_jev"})
    r = client.post(f"/api/ai/threads/{t['id']}/messages", json={"content": "second", "privacy": "allow_jev"})
    rec = next(d for e, d in sse(r.text) if e == "receipt")
    assert [s["label"] for s in rec["route"]] == ["Auto", "Jev", "Local Deep"]
    assert rec["privacy"] == "local_jev" and rec["decision"]["decided_by"] == "jev"
    assert seen[1].headers["x-lif-data-class"] == "PUBLIC"
    msgs = json.loads(seen[1].content)["messages"]
    assert [m["content"] for m in msgs] == ["first", "Hello world", "second"]


@pytest.mark.parametrize("resp,title", [
    (httpx.Response(429, json={"error": {"message": "queue timeout waiting for a model slot (primary-workload yield)",
                                         "type": "capacity"}}), "Waiting for Primary workload to finish"),
    (httpx.Response(429, json={"error": {"message": "queue timeout waiting for a model slot",
                                         "type": "capacity"}}), "Local AI is busy"),
    (httpx.Response(503, json={"error": {"message": "no local model is deployed for local/vision (capacity; see "
                                                    "/v1/capabilities)", "type": "capacity"}}),
     "No local vision model is installed yet"),
    (httpx.Response(503, json={"error": {"message": "all models for local/default are unavailable: x: down",
                                         "type": "capacity"}}), "Balanced models are offline"),
    # upstream 4xx comes back as HTTP 200 with raw JSON error lines
    (httpx.Response(200, headers=HAPPY_HEADERS, content=b'{"error":{"code":400,"message":"bad template",'
                                                         b'"type":"invalid_request_error"}}\n\n'),
     "The model couldn't accept this request"),
])
def test_ask_gateway_errors_are_human(client, env, resp, title):
    gateway(resp, [])
    mode = "vision" if "vision" in title else "balanced" if "Balanced" in title else "auto"
    t = client.post("/api/ai/threads", json={"mode": mode}).json()
    evs = sse(client.post(f"/api/ai/threads/{t['id']}/messages", json={"content": "hi", "mode": mode}).text)
    err = next(d for e, d in evs if e == "error")
    assert err["title"] == title and err["impact"] and err["next_step"]
    assert "HTTP" not in err["title"] and evs[-1][0] == "done"
    if resp.status_code == 429:
        assert {"label": "Retry", "action": "retry"} in err["actions"]
    m = client.get(f"/api/ai/threads/{t['id']}").json()["messages"][1]
    assert m["status"] == "error" and m["error"]["title"] == title


def test_ask_clamp_reported_only_while_gpusched_imminent(client, env, monkeypatch):
    monkeypatch.setattr("lif.gpu.workload.enabled", lambda: True)
    body = chunk("cut") + f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'length'}]})}\n\n" + \
        "data: [DONE]\n\n"
    gateway(httpx.Response(200, headers=HAPPY_HEADERS, content=body.encode()), [])
    t = client.post("/api/ai/threads", json={}).json()
    rec = next(d for e, d in sse(client.post(f"/api/ai/threads/{t['id']}/messages", json={"content": "a"}).text)
               if e == "receipt")
    assert rec["clamped"] is False                                   # LOW: a 'length' stop is the model's own
    env["snap"].raw["gpu"] = {"state": "IMMINENT"}
    rec = next(d for e, d in sse(client.post(f"/api/ai/threads/{t['id']}/messages", json={"content": "b"}).text)
               if e == "receipt")
    assert rec["clamped"] is True


def test_ask_midstream_failure_keeps_text(client, env):
    body = chunk("Partial") + 'data: {"error":{"message":"upstream stream failed","type":"upstream"}}\n\n'
    gateway(httpx.Response(200, headers=HAPPY_HEADERS, content=body.encode()), [])
    t = client.post("/api/ai/threads", json={}).json()
    evs = sse(client.post(f"/api/ai/threads/{t['id']}/messages", json={"content": "hi"}).text)
    assert next(d for e, d in evs if e == "error")["title"] == "The model stopped mid-answer"
    m = client.get(f"/api/ai/threads/{t['id']}").json()["messages"][1]
    assert m["content"] == "Partial" and m["status"] == "error"


def test_ask_without_gateway_key(client, env, monkeypatch):
    monkeypatch.delenv("LIF_CONSOLE_GATEWAY_KEY")
    monkeypatch.setattr(ai.settings, "gateway_key", lambda: None)
    t = client.post("/api/ai/threads", json={}).json()
    evs = sse(client.post(f"/api/ai/threads/{t['id']}/messages", json={"content": "hi"}).text)
    assert next(d for e, d in evs if e == "error")["title"] == "Ask isn't set up yet"


def test_ask_attachments(client, env):
    seen: list[httpx.Request] = []
    gateway(happy_stream(), seen)
    t = client.post("/api/ai/threads", json={}).json()
    r = client.post(f"/api/ai/threads/{t['id']}/messages", json={"content": "Review this", "attachments": [
        {"name": "parse.py", "kind": "code", "size": 20, "text": "print('hi')"},
        {"name": "shot.png", "kind": "image", "size": 2000},
        {"name": "spec.pdf", "kind": "pdf", "size": 3000}]})
    assert r.status_code == 200
    prompt = json.loads(seen[0].content)["messages"][-1]["content"]
    assert prompt.startswith("Review this") and "--- File: parse.py (code) ---\nprint('hi')" in prompt
    assert "shot.png" not in prompt and "spec.pdf" not in prompt
    atts = client.get(f"/api/ai/threads/{t['id']}").json()["messages"][0]["attachments"]
    assert [a["included"] for a in atts] == [True, False, False]
    assert "couldn't read the image" in atts[1]["note"] and "PDF" in atts[2]["note"]
    assert all("text" not in a for a in atts)                        # file text stays server-side
    only_image = client.post(f"/api/ai/threads/{t['id']}/messages",
                             json={"content": "", "attachments": [{"name": "a.png", "kind": "image", "size": 1}]})
    assert only_image.status_code == 422 and only_image.json()["error"]["title"] == "Type a prompt first"


def test_ask_continues_after_disconnect(env):
    """The browser leaves mid-answer: the producer still finishes and persists (Open on desktop)."""
    gateway(happy_stream(), [])
    t = threads.create_thread(ADMIN.id, "auto", "local_only")
    threads.add_message(t.id, "user", "hi")
    msg = threads.add_message(t.id, "assistant", "", status="streaming")

    async def go() -> None:
        run = ai._Run(thread_id=t.id, message_id=msg.id, user_id=ADMIN.id, attached=False)
        ai._RUNS[msg.id] = run
        await ai.produce(run, {"model": "local/auto", "stream": True, "messages": []}, mode="auto",
                         privacy="local_only")
        assert run.queue.empty()                       # nothing queued for a browser that left
    asyncio.run(go())
    m = threads.get_thread(ADMIN.id, t.id).messages[1]
    assert m.status == "done" and m.content == "Hello world" and m.receipt.alias == "local/fast"
    assert msg.id not in ai._RUNS


def test_ask_cancel(env):
    started = asyncio.Event

    async def go() -> None:
        gate = started()

        async def slow():
            yield chunk("Hi").encode()
            gate.set()
            await asyncio.sleep(30)
            yield chunk("never").encode()

        ai._transport = httpx.MockTransport(lambda req: httpx.Response(200, headers=HAPPY_HEADERS, content=slow()))
        t = threads.create_thread(ADMIN.id, "auto", "local_only")
        msg = threads.add_message(t.id, "assistant", "", status="streaming")
        run = ai._Run(thread_id=t.id, message_id=msg.id, user_id=ADMIN.id)
        ai._RUNS[msg.id] = run
        run.task = asyncio.create_task(ai.produce(run, {"model": "local/auto", "stream": True, "messages": []},
                                                  mode="auto", privacy="local_only"))
        await asyncio.wait_for(gate.wait(), 5)
        await asyncio.sleep(0.05)
        run.cancel()
        await asyncio.wait_for(run.task, 5)
        m = threads.get_thread(ADMIN.id, t.id).messages[0]
        assert m.status == "cancelled" and m.content == "Hi"
    asyncio.run(go())


def test_threads_scope_and_delete(client, env):
    gateway(happy_stream(), [])
    t = client.post("/api/ai/threads", json={"title": "My notes"}).json()
    assert t["title"] == "My notes"
    client.post(f"/api/ai/threads/{t['id']}/messages", json={"content": "something else entirely"})
    assert client.get(f"/api/ai/threads/{t['id']}").json()["title"] == "My notes"    # a chosen title is kept
    env["user"] = User(id="someone_else", name="x", role="admin", perms=list(auth.ALL_PERMS))
    assert client.get(f"/api/ai/threads/{t['id']}").status_code == 404
    assert client.get("/api/ai/threads").json() == []
    env["user"] = ADMIN
    assert client.delete(f"/api/ai/threads/{t['id']}").json()["ok"] is True
    assert client.get(f"/api/ai/threads/{t['id']}").json()["error"]["title"] == "Conversation not found"


def test_title_and_context_helpers(env):
    assert threads.title_from("\n\n# Explain the K3s scheduler in detail please because I keep forgetting it\nmore") \
        .endswith("…")
    assert threads.title_from("short") == "short"
    assert ai.friendly_model("qwen2-5-coder-14b-instruct-q4km-gpu").startswith("Qwen2 5 Coder 14B")


def test_capabilities(client, env):
    caps = client.get("/api/ai/capabilities").json()
    modes = {m["mode"]: m for m in caps["modes"]}
    assert list(modes) == ["auto", "fast", "balanced", "deep", "code", "vision"]
    assert modes["fast"]["blurb"] == "Best for low-latency requests." and modes["fast"]["alias"] == "local/fast"
    assert modes["vision"]["available"] is False and "vision" in modes["vision"]["reason"].lower()
    assert modes["balanced"]["available"] is True and "Primary workload" in modes["balanced"]["reason"]
    assert "BLERBZ" not in modes["balanced"]["reason"]
    assert caps["external_allowed"] is False
    assert [p["value"] for p in caps["privacy_options"]] == ["local_only", "allow_jev"]


# ── jobs ─────────────────────────────────────────────────────────────────────────────────────

def batch_view(bid: str, state: str, reason: str = "", **over: Any) -> dict[str, Any]:
    v = {"id": bid, "owner": "batch", "model": "local/batch", "priority": 6, "priority_source": "caller",
         "description": f"job {bid}", "state": state, "reason": reason, "created": time.time() - 600,
         "finished": None, "counts": {"pending": 5, "running": 0, "succeeded": 5, "failed": 0}, "total": 10,
         "progress": 0.5}
    v.update(over)
    return v


JOB_ROUTES = {
    ("batch", "GET", "/v1/batch"): {"data": [
        batch_view("bj_000000000001", "queued", "waiting: primary workload IMMINENT (1 production lease(s) live): "
                   "batch/eval paused"),
        batch_view("bj_000000000002", "queued", "waiting: batch paused by operator (operator)"),
        batch_view("bj_000000000003", "paused", "paused by owner"),
        batch_view("bj_000000000004", "running"),
        batch_view("bj_000000000005", "completed", finished=time.time() - 60,
                   counts={"succeeded": 8, "failed": 2}, progress=1.0)]},
    ("batch", "GET", "/v1/batch/stats"): {"paused": True, "paused_reason": "operator"},
    ("controller", "GET", "/v1/discovery/runs"): {"running": None, "runs": [
        {"id": 4, "ts": time.time() - 3600, "finished": time.time() - 3590, "status": "succeeded",
         "funnel": {"categories": {"coding": {"listed": 2000, "after_deterministic": 312, "after_screening": 9,
                                              "shortlisted": ["a/b", "c/d"], "jev_calls": 64,
                                              "providers": {"jev": 9}}}, "total_ms": 6544}, "error": None},
        {"id": 3, "ts": time.time() - 7200, "finished": None, "status": "running", "funnel": {}}]},
    ("controller", "GET", "/v1/benchmarks"): {"benchmarks": [
        {"id": 1, "model": "qwen3-4b-instruct-2507-q4km-cpu", "ts": time.time() - 100, "suite": "core",
         "summary": {"quality": 0.8, "errors": 0}}]},
    ("controller", "GET", "/v1/overview"): {"tasks": {"download:m1": "running", "discovery": "done"}},
}


def test_jobs_list(client, env):
    upstreams(JOB_ROUTES)
    r = client.get("/api/jobs").json()
    g = r["groups"]
    by_id = {j["id"]: j for grp in g.values() for j in grp}
    w = by_id["batch:bj_000000000001"]
    assert w["status"] == "waiting" and w["resumes_automatically"] is True and "Primary workload" in w["reason"]
    assert "BLERBZ" not in (w["reason"] or "")
    assert by_id["batch:bj_000000000002"]["status"] == "paused"
    assert by_id["batch:bj_000000000003"]["actions"] == ["resume", "cancel"]
    assert by_id["batch:bj_000000000005"]["status_label"] == "Finished with 2 failures"
    assert by_id["discovery:4"]["status_label"] == "Finished — 2 candidates shortlisted"
    assert by_id["discovery:3"]["status_label"] == "Interrupted"      # 'running' row, but nothing is running
    assert by_id["download:m1"]["status"] == "running"
    assert "benchmark:qwen3-4b-instruct-2507-q4km-cpu:1" in by_id
    assert {"scheduled:sqlite-backup", "scheduled:longhorn-backup", "scheduled:auto-discovery"} <= set(by_id)
    assert r["batch_paused"] is True
    s = r["summary"]
    assert s == {"running": 2, "queued": 0, "waiting": 3, "failed_24h": 1}     # scheduled work is not queued work
    only = client.get("/api/jobs?status=failed").json()["groups"]
    assert only["running"] == [] and only["queued"] == []


def test_jobs_degrade_when_batch_is_down(client, env):
    upstreams({k: v for k, v in JOB_ROUTES.items() if k[0] != "batch"})
    r = client.get("/api/jobs").json()
    assert "batch service didn't answer" in r["note"]
    assert any(j["id"] == "discovery:4" for j in r["groups"]["completed"])


def test_job_actions(client, env):
    calls: list[httpx.Request] = []
    one = batch_view("bj_000000000004", "running")
    upstreams({("batch", "GET", "/v1/batch/bj_000000000004"): one,
               ("batch", "POST", "/v1/batch/bj_000000000004/pause"): {**one, "state": "paused", "reason": "paused by owner"},
               ("batch", "DELETE", "/v1/batch/bj_000000000004"): {**one, "state": "cancelled"},
               **JOB_ROUTES}, calls)
    r = client.post("/api/jobs/batch:bj_000000000004/pause")
    assert r.status_code == 200 and r.json()["status"] == "paused"
    r = client.post("/api/jobs/batch:bj_000000000004/cancel")
    assert r.json()["status"] == "cancelled" and calls[-1].method == "DELETE"
    r = client.post("/api/jobs/batch:bj_000000000004/resume")      # running jobs can't be resumed
    assert r.status_code == 409 and "can't be resumed" in r.json()["error"]["title"]
    r = client.post("/api/jobs/discovery:4/pause")
    assert r.status_code == 409 and r.json()["error"]["title"] == "This job can't be paused"
    env["user"] = User(id="v", name="viewer", role="device", perms=["read"])
    assert client.post("/api/jobs/batch:bj_000000000004/pause").status_code == 403


def test_pause_all_keeps_maintenance_or(client, env):
    calls: list[httpx.Request] = []
    upstreams({("controller", "GET", "/v1/settings"): {"maintenance": True, "batch_paused": True},
               ("controller", "POST", "/v1/settings"): {"ok": True}}, calls)
    # pause-all also asks the poller to refresh, so background reads land in `calls` too
    last_post = lambda: json.loads([c for c in calls if c.method == "POST"][-1].content)  # noqa: E731
    r = client.post("/api/jobs/batch/pause-all", json={"paused": False})
    assert r.status_code == 200 and "maintenance mode is on" in r.json()["message"]
    assert last_post() == {"batch_paused": False, "maintenance": True}
    r = client.post("/api/jobs/batch/pause-all", json={"paused": True})
    assert last_post() == {"batch_paused": True}
    env["user"] = DEVICE                                          # phones may pause batch (§99)
    assert client.post("/api/jobs/batch/pause-all", json={"paused": True}).status_code == 200


# ── agents and approvals ─────────────────────────────────────────────────────────────────────

AGENT_ROUTES = {
    ("controller", "GET", "/v1/discovery/runs"): JOB_ROUTES[("controller", "GET", "/v1/discovery/runs")],
    ("controller", "GET", "/v1/activity"): {"activity": [
        {"seq": 12, "ts": 1000.0, "kind": "state_changed", "subject": "m2", "actor": "operator",
         "detail": {"frm": "STAGED", "to": "VALIDATING"}},
        {"seq": 13, "ts": 1100.0, "kind": "load_test_passed", "subject": "m2", "detail": {"load_sec": 40}},
        {"seq": 14, "ts": 1500.0, "kind": "benchmark_completed", "subject": "m2",
         "detail": {"quality": 0.87, "decode_tps_p50": 18.5, "errors": 0}},
        {"seq": 15, "ts": 1510.0, "kind": "comparison_complete", "subject": "m2",
         "detail": {"recommendation": "CANARY", "incumbent": "qwen3-4b-instruct-2507-q4km-cpu",
                    "jev": {"improves": "yes", "confidence": 0.8}}}]},
    ("controller", "GET", "/v1/de/cycle"): {"note": "no improvement cycle has run yet"},
    ("controller", "GET", "/v1/de/traces"): {"runs": [{"run_id": "r1", "agent": "claude-code", "workflow": "edit",
                                                        "t0": 900.0, "ops": 5, "jev": 1, "gen": 2, "code": 2,
                                                        "human": 0}]},
    ("controller", "GET", "/v1/de/traces/r1"): {"run_id": "r1", "operations": [
        {"seq": 1, "ts": 901.0, "kind": "decision", "name": "tool-selection", "classification": "JEV_CANDIDATE",
         "executor": "jev", "output_label": "edit"}]},
    ("controller", "GET", "/v1/de/human"): {"queue": [
        {"id": 20, "ts": 2000.0, "decision_ref": "model-advance/v1", "status": "pending",
         "package": {"decision": "model-advance", "instructions": "Should this model advance?",
                     "labels": ["advance", "hold"], "criteria": {"advance": "Clearly better"},
                     "state": {"secret_input": "PRIVATE-STATE-VALUE"}, "reason": "shadow_sample",
                     "data_class": "CONFIDENTIAL", "jev": {"answer": "advance", "confidence": 0.62}}}]},
    ("controller", "POST", "/v1/models/refresh"): {"status": "started", "task": "discovery"},
    ("controller", "POST", "/v1/de/human/20"): {"ticket": 20, "status": "answered"},
}


def test_agents_overview(client, env):
    upstreams(AGENT_ROUTES)
    r = client.get("/api/agents").json()
    runs = {x["id"]: x for x in r["running"] + r["recent"]}
    scout = runs["scout:4"]
    assert scout["agent"] == "Model Scout" and scout["status"] == "done" and scout["source"] == "discovery"
    labels = [s["label"] for s in scout["steps"]]
    assert "Discovering: 2,000 models found on Hugging Face" in labels and "2 candidates shortlisted" == scout["result"]
    assert runs["scout:3"]["status"] == "failed" and "Interrupted" in runs["scout:3"]["result"]
    ev = runs["eval:14"]
    assert ev["agent"] == "Evaluator" and ev["source"] == "benchmark"
    assert ev["result"] == "Better than the model in use: ready for a trial"
    assert [s["label"] for s in ev["steps"]][:2] == ["Starting a test server", "Test server ready (loaded in 40 s)"]
    assert runs["trace:r1"]["source"] == "trace" and "don't record" in runs["trace:r1"]["result"]
    assert not any(x["agent"] == "Decision Tuner" for x in runs.values())     # no cycle has run: nothing invented
    assert not any("Code Agent" in x["agent"] for x in runs.values())
    cat = {c["id"]: c for c in r["catalog"]}
    assert set(cat) == {"model-scout", "evaluator"} and cat["model-scout"]["runnable"]
    assert cat["evaluator"]["perm"] == "models.operate"
    calls: list[httpx.Request] = []
    upstreams(AGENT_ROUTES, calls)
    for bad in ("trace:..%2F..%2Fsettings", "trace:a.b", "trace:..%2Fv1%2Fsettings"):
        assert client.get(f"/api/agents/{bad}").status_code == 404
    assert calls == []                                               # never reaches the controller
    assert client.get("/api/knowledge/objects/%2E%2E").status_code == 404
    detail = client.get("/api/agents/trace:r1").json()
    assert detail["steps"][0]["label"] == "tool-selection" and "quick decision" in detail["steps"][0]["detail"]
    env["user"] = DEVICE
    cat = {c["id"]: c for c in client.get("/api/agents").json()["catalog"]}
    assert cat["evaluator"]["runnable"] is False and cat["evaluator"]["why_not"] == "Needs an admin session."


def test_agents_run(client, env):
    calls: list[httpx.Request] = []
    upstreams(AGENT_ROUTES, calls)
    r = client.post("/api/agents/run", json={"agent": "model-scout", "params": {"categories": ["coding"]}})
    assert r.status_code == 200 and "Model Scout started" in r.json()["message"]
    assert json.loads(calls[-1].content) == {"categories": ["coding"]}
    assert client.post("/api/agents/run", json={"agent": "code-agent"}).status_code == 404
    bad = client.post("/api/agents/run", json={"agent": "model-scout", "params": {"categories": ["poetry"]}})
    assert bad.status_code == 422
    env["user"] = DEVICE
    r = client.post("/api/agents/run", json={"agent": "evaluator", "params": {"candidate": "m2"}})
    assert r.status_code == 403 and r.json()["error"]["title"] == "Not allowed from this session"


def test_approvals(client, env):
    calls: list[httpx.Request] = []
    upstreams(AGENT_ROUTES, calls)
    items = client.get("/api/approvals").json()
    review = next(a for a in items if a["id"] == "review:20")
    assert review["kind"] == "review" and review["blocking"] is False
    assert review["impact"].startswith("Nothing is waiting on this answer")
    assert [o["value"] for o in review["options"]] == ["advance", "hold"]
    assert review["options"][0]["description"] == "Clearly better"
    assert "Jev suggested “advance” (62% confident)" in review["why"]
    assert "PRIVATE-STATE-VALUE" not in json.dumps(items)          # ticket inputs never leave the console
    assert client.post("/api/approvals/review:20", json={"answer": "maybe"}).status_code == 422
    ok = client.post("/api/approvals/review:20", json={"answer": "advance"})
    assert ok.status_code == 200 and json.loads(calls[-1].content) == {"answer": "advance"}
    assert calls[-1].url.path == "/v1/de/human/20"
    # already answered elsewhere: re-read says it's gone → refuse instead of overwriting
    upstreams({**AGENT_ROUTES, ("controller", "GET", "/v1/de/human"): {"queue": []}})
    r = client.post("/api/approvals/review:20", json={"answer": "hold"})
    assert r.status_code == 409 and r.json()["error"]["title"] == "This review was already answered"


def test_pairing_approval_card(client, env):
    upstreams({("controller", "GET", "/v1/de/human"): {"queue": []}})
    now = time.time()
    with db.tx() as c:
        c.execute("INSERT INTO users(id, name, role, created_at) VALUES(?,?,?,?)", ("u_owner", "owner", "admin", now))
        c.execute("INSERT INTO pairings(id, token_hash, created_by, created_at, expires_at, status, code, device_name,"
                  " claimed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  ("p1", "h", "u_owner", now, now + 120, "claimed", "123456", "Pixel", now))
    items = client.get("/api/approvals").json()
    assert items[0]["id"] == "pairing:p1" and items[0]["blocking"] is True
    env["user"] = DEVICE                                         # phones don't see or decide pairings
    assert client.get("/api/approvals").json() == []
    assert client.post("/api/approvals/pairing:p1", json={"answer": "approve"}).status_code == 403


# ── knowledge ────────────────────────────────────────────────────────────────────────────────

def _walk_keys(x: Any) -> list[str]:
    if isinstance(x, dict):
        return [v for k, v in x.items() if k == "key" and isinstance(v, str)] + \
            [k2 for v in x.values() for k2 in _walk_keys(v)]
    if isinstance(x, list):
        return [k2 for v in x for k2 in _walk_keys(v)]
    return []


def test_knowledge_bundled_public_only(client, env):
    home = client.get("/api/knowledge").json()
    assert home["mode"] == "bundled" and home["connected"] is True and home["decisions"]
    found = client.get("/api/knowledge/search", params={"q": "Why are we using K3s"}).json()
    assert found and found[0]["type"] == "decision" and "K3s" in found[0]["title"]
    rec = client.get(f"/api/knowledge/objects/{found[0]['key']}").json()
    assert rec["kind"] == "decision" and rec["decision"]["status"] == "In effect" and rec["decision"]["why"]
    other = client.get(f"/api/knowledge/objects/{home['assumptions'][0]['key']}").json()
    assert other["kind"] == "object" and other["object"]["title"]
    everything = [home, found, rec, other]
    for q in ("incident", "operations", "OOM", "GPU reserve", "primary workload"):
        everything.append(client.get("/api/knowledge/search", params={"q": q}).json())
    keys = _walk_keys(everything)
    assert keys
    assert not any(k.split("::")[0] == "lif-operations" for k in keys)
    text = json.dumps(everything)
    assert "/home/" not in text and "/app/" not in text
    assert client.get("/api/knowledge/objects/lif-operations::anything").status_code == 404


def test_knowledge_filters_confidential(tmp_path, client, env, monkeypatch):
    root = tmp_path / "ws"
    shutil.copytree(LIF / "knowledge" / "registry", root / "registry")

    def write(p: Path, text: str) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text).lstrip())

    write(root / "workspace.yaml", """
        name: t
        repos:
          - path: repos/pub
          - path: repos/secret
    """)
    write(root / "repos/pub/repo.yaml", """
        name: pub
        version: 0.1.0
        kind: project
        data_class: PUBLIC
        dependencies:
          - {name: knowledge-governance, version: ^1.0}
    """)
    write(root / "repos/secret/repo.yaml", """
        name: secret
        version: 0.1.0
        kind: project
        data_class: CONFIDENTIAL
        dependencies:
          - {name: knowledge-governance, version: ^1.0}
    """)
    write(root / "repos/pub/knowledge/open-note.md", """
        ---
        type: assumption
        statement: Zebra telemetry is public. See /home/someone/labzilla/lif/docs/X.md for details.
        status: active
        ---
        Zebra body text.
    """)
    write(root / "repos/pub/knowledge/hidden-note.md", """
        ---
        type: assumption
        statement: Zebra secret plan inside a public repo.
        status: active
        data_class: CONFIDENTIAL
        ---
    """)
    write(root / "repos/secret/knowledge/private-note.md", """
        ---
        type: assumption
        statement: Zebra private incident details.
        status: active
        ---
    """)
    monkeypatch.setenv("LIF_KNOWLEDGE_ROOT", str(root))
    hits = client.get("/api/knowledge/search", params={"q": "zebra"}).json()
    assert [h["key"] for h in hits] == ["pub::open-note"]
    assert client.get("/api/knowledge/objects/pub::hidden-note").status_code == 404
    assert client.get("/api/knowledge/objects/secret::private-note").status_code == 404
    assert client.get("/api/knowledge/objects/private-note").status_code == 404
    home = client.get("/api/knowledge").json()
    assert {h["key"] for h in home["assumptions"]} == {"pub::open-note"}
    summary = home["assumptions"][0]["summary"]
    assert "lif/docs/X.md" in summary and "/home/" not in summary
    assert knowledge.strip_paths("see /app/knowledge/repos/x/a.md and /home/u/labzilla/lif/a.py") == \
        "see knowledge/repos/x/a.md and lif/a.py"


def test_knowledge_service_mode_allowlist(client, env, monkeypatch):
    monkeypatch.setenv("LIF_KNOWLEDGE_URL", "http://knowledge.ai-system.svc:8080")
    rows = [{"key": "local-intelligence-fabric::a", "type": "decision", "title": "Public", "status": "accepted",
             "repo": "local-intelligence-fabric", "updated": "2026-10-01", "snippet": "[k3s] public",
             "path": "/home/x/labzilla/a.md"},
            {"key": "lif-operations::b", "type": "incident", "title": "Private", "repo": "lif-operations",
             "updated": "2026-10-01", "snippet": "private"},
            {"key": "local-intelligence-fabric::c", "type": "note", "title": "Labelled confidential",
             "repo": "local-intelligence-fabric", "updated": "2026-10-01", "snippet": "[k3s] hidden"}]
    obj = lambda k, cls: {"key": k, "repo": "local-intelligence-fabric", "fields": {"data_class": cls}}  # noqa: E731
    upstreams({("knowledge", "GET", "/v1/knowledge/search"): {"results": rows},
               # search hits carry no fields: the console reads each object to check its own data class
               ("knowledge", "GET", "/v1/knowledge/objects/local-intelligence-fabric::a"):
                   obj("local-intelligence-fabric::a", "PUBLIC"),
               ("knowledge", "GET", "/v1/knowledge/objects/local-intelligence-fabric::c"):
                   obj("local-intelligence-fabric::c", "CONFIDENTIAL")})
    hits = client.get("/api/knowledge/search", params={"q": "k3s"}).json()
    assert [h["key"] for h in hits] == ["local-intelligence-fabric::a"] and hits[0]["summary"] == "k3s public"


def test_save_note_is_read_only(client, env):
    r = client.post("/api/knowledge/notes", json={"title": "x", "body": "y"})
    assert r.status_code == 409
    assert r.json()["error"]["title"] == "Knowledge is read-only until the knowledge service is deployed"
    env["user"] = DEVICE
    assert client.post("/api/knowledge/notes", json={"title": "x", "body": "y"}).status_code == 403


def test_decision_sentence_never_a_bare_token():
    from lif.console.routes.knowledge import decision_sentence
    title = "Use K3s as the orchestration layer on the single DGX node"
    assert decision_sentence(title, "k3s") == f"{title} (selected: k3s)"
    assert decision_sentence(title, ["k3s", "longhorn"]) == f"{title} (selected: k3s, longhorn)"
    assert decision_sentence(title, None) == title
    long = "Run one replica of the console with SQLite on a Longhorn volume"
    assert decision_sentence(title, long) == long


def test_scout_run_survives_odd_upstream_values():
    from lif.console.routes import agents as ag
    run = {"id": 1, "ts": 1.0, "status": "succeeded", "funnel": {"categories": {
        "coding": {"listed": "n/a", "after_deterministic": None, "shortlisted": "x", "providers": {"jev": "?"}}}}}
    assert ag.scout_run(run, None).agent == "Model Scout"
    assert ag.scout_run({"id": 2, "funnel": "garbage"}, None).agent == "Model Scout"


def test_review_question_is_readable_and_never_leaks_private_state():
    from lif.console.routes.agents import review_approval
    pkg = {"decision": "model-advance/v1", "labels": ["yes", "no"], "reason": "shadow_sample",
           "instructions": "Judge whether `model.id` suits `category.name` (newer: `comparison.newer`).",
           "criteria": {"yes": "`model.id` is an official release"},
           "state": {"model": {"id": "acme/Coder-7B"}, "category.name": "coding", "comparison": {"newer": True}}}
    pub = review_approval({"id": 1, "status": "pending", "package": {**pkg, "data_class": "PUBLIC"}})
    assert "`" not in pub.action and pub.title == "Review a decision: model advance"
    assert "“acme/Coder-7B”" in pub.action and "“coding”" in pub.action and "newer: yes" in pub.action
    assert "“acme/Coder-7B”" in (pub.options[0].description or "")
    priv = review_approval({"id": 2, "status": "pending", "package": {**pkg, "data_class": "CONFIDENTIAL"}})
    assert "acme" not in priv.action and "coding" not in priv.action and "`" not in priv.action
    assert "the model id" in priv.action
    nodc = review_approval({"id": 3, "status": "pending", "package": pkg})      # missing class → private
    assert "acme" not in nodc.action


# ── live history: thread events for every session of the owner (§67–§70) ──────────────────────

def _thread_events(env, kind: str | None = None) -> list[Any]:
    return [d for typ, d, _ in env["hub"].events if typ == "thread" and (kind is None or d.kind == kind)]


def test_thread_events_follow_the_owner_everywhere(client, env):
    gateway(happy_stream(), [])
    env["user"] = DEVICE                                    # started on the phone
    t = client.post("/api/ai/threads", json={"mode": "auto"}).json()
    created = _thread_events(env, "upsert")[-1]
    assert created.thread_id == t["id"] and created.summary.origin_device == "Phone"
    assert created.summary.active is False and created.summary.title == "New conversation"

    client.post(f"/api/ai/threads/{t['id']}/messages", json={"content": "How warm is the GPU?"})
    ups = [e for e in _thread_events(env, "upsert") if e.thread_id == t["id"]]
    # the new prompt (title, preview, "Answering…"), then the finished row
    assert [e.summary.active for e in ups[-2:]] == [True, False]
    assert ups[-2].summary.title == "How warm is the GPU?" and ups[-2].summary.preview == "How warm is the GPU?"
    msgs = _thread_events(env, "message")
    assert msgs[0].status == "streaming" and msgs[-1].status == "done" and msgs[-1].content == "Hello world"
    # every thread event goes to this owner's sessions only (desktop and paired phones share the user id)
    assert {aud for typ, _, aud in env["hub"].events if typ == "thread"} == {"user:u_owner"}

    env["user"] = ADMIN                                     # the desktop sees where it came from
    row = client.get("/api/ai/threads").json()[0]
    assert row["origin_device"] == "Phone" and row["active"] is False


def test_thread_list_active_comes_from_live_runs_not_the_database(client, env):
    t = threads.create_thread(ADMIN.id, "auto", "local_only")
    threads.add_message(t.id, "user", "hi")
    msg = threads.add_message(t.id, "assistant", "", status="streaming")      # left over by a restart
    assert client.get("/api/ai/threads").json()[0]["active"] is False
    ai._RUNS[msg.id] = ai._Run(thread_id=t.id, message_id=msg.id, user_id=ADMIN.id)
    try:
        assert client.get("/api/ai/threads").json()[0]["active"] is True
    finally:
        ai._RUNS.pop(msg.id, None)


def test_rename_and_delete_publish_and_stay_scoped(client, env):
    t = client.post("/api/ai/threads", json={"title": "First"}).json()
    before = client.get("/api/ai/threads").json()[0]["updated_at"]
    r = client.patch(f"/api/ai/threads/{t['id']}", json={"title": "  GPU   notes  "})
    assert r.status_code == 200 and r.json()["title"] == "GPU notes"
    assert client.get("/api/ai/threads").json()[0]["updated_at"] == before          # renaming doesn't reorder
    assert _thread_events(env, "upsert")[-1].summary.title == "GPU notes"
    assert client.patch(f"/api/ai/threads/{t['id']}", json={"title": "  "}).status_code == 422
    assert client.patch(f"/api/ai/threads/{t['id']}", json={"title": "x" * 121}).status_code == 422

    env["user"] = User(id="someone_else", name="x", role="admin", perms=list(auth.ALL_PERMS))
    n = len(env["hub"].events)
    assert client.patch(f"/api/ai/threads/{t['id']}", json={"title": "mine now"}).status_code == 404
    assert client.delete(f"/api/ai/threads/{t['id']}").status_code == 404
    assert len(env["hub"].events) == n                       # nothing published for someone else's thread

    env["user"] = DEVICE                                     # a paired phone may delete its owner's thread
    assert client.delete(f"/api/ai/threads/{t['id']}").json()["ok"] is True
    gone = _thread_events(env, "deleted")[-1]
    assert gone.thread_id == t["id"] and env["hub"].events[-1][2] == "user:u_owner"


def test_quota_pruned_threads_are_announced(client, env, monkeypatch):
    gateway(happy_stream(), [])
    old = client.post("/api/ai/threads", json={}).json()
    cur = client.post("/api/ai/threads", json={}).json()
    monkeypatch.setattr(threads, "enforce_quota", lambda user_id, keep: [old["id"]])
    client.post(f"/api/ai/threads/{cur['id']}/messages", json={"content": "hi"})
    assert old["id"] in [e.thread_id for e in _thread_events(env, "deleted")]


def test_thread_events_reach_admin_and_device_sessions_but_not_another_user():
    """The real hub: the owner's desktop (admin) and phone (device, same user id) both get thread events;
    another person's session never does."""
    async def run() -> None:
        hub = events.Hub()
        other = User(id="u_guest", name="guest", role="admin", perms=list(auth.ALL_PERMS))
        subs = {name: hub.subscribe(u, session=name) for name, u in
                (("desktop", ADMIN), ("phone", DEVICE), ("guest", other))}
        for g in subs.values():
            await asyncio.wait_for(g.__anext__(), 1)                 # retry: frame
        await asyncio.sleep(0)
        hub.publish("thread", ai.ThreadEvent(kind="deleted", thread_id="th_x"), audience=f"user:{ADMIN.id}")
        hub.publish("status", {"health": "healthy"})
        for name in ("desktop", "phone"):
            frame = await asyncio.wait_for(subs[name].__anext__(), 1)
            assert frame.startswith("event: thread\n") and '"thread_id":"th_x"' in frame
        frame = await asyncio.wait_for(subs["guest"].__anext__(), 1)
        assert frame.startswith("event: status")                     # skipped the thread event
        hub.close()
    asyncio.run(run())


# ── vision: images in Ask ─────────────────────────────────────────────────────────────────────

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def data_url(raw: bytes, mime: str = "image/jpeg") -> str:
    import base64
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def image(name: str = "photo.jpg", raw: bytes = JPEG, mime: str = "image/jpeg", **over: Any) -> dict[str, Any]:
    return {"name": name, "kind": "image", "size": len(raw), "image": data_url(raw, mime), "width": 1024,
            "height": 768, **over}


def test_ask_images_are_sent_as_content_parts_and_never_stored(client, env):
    seen: list[httpx.Request] = []
    gateway(happy_stream(), seen)
    t = client.post("/api/ai/threads", json={}).json()
    r = client.post(f"/api/ai/threads/{t['id']}/messages", json={
        "content": "What is on this board?", "mode": "auto", "privacy": "allow_jev",
        "attachments": [image(), image("diagram.png", PNG, "image/png")]})
    assert r.status_code == 200 and "error" not in [e for e, _ in sse(r.text)]
    req = seen[0]
    assert req.headers["x-lif-data-class"] == "CONFIDENTIAL"         # images never go to Jev
    body = json.loads(req.content)
    assert body["model"] == "local/auto"                             # the gateway routes auto + images to vision
    parts = body["messages"][-1]["content"]
    assert parts[0] == {"type": "text", "text": "What is on this board?"}
    assert parts[1] == {"type": "image_url", "image_url": {"url": data_url(JPEG)}}
    assert parts[2]["image_url"]["url"].startswith("data:image/png;base64,")
    # only name, kind, size and dimensions are kept
    raw = db.one("SELECT attachments_json FROM messages WHERE thread_id=? AND role='user'", (t["id"],))
    assert "base64" not in raw["attachments_json"] and "/9j/" not in raw["attachments_json"]
    atts = client.get(f"/api/ai/threads/{t['id']}").json()["messages"][0]["attachments"]
    assert [(a["name"], a["included"], a["width"], a["height"]) for a in atts] == [
        ("photo.jpg", True, 1024, 768), ("diagram.png", True, 1024, 768)]
    assert all("not kept" in a["note"] for a in atts)
    # the next turn replays the image turn as text
    gateway(happy_stream(), seen)
    client.post(f"/api/ai/threads/{t['id']}/messages", json={"content": "And the second one?"})
    replay = json.loads(seen[-1].content)["messages"]
    assert replay[0]["content"] == ("What is on this board?\n\n[Earlier image, not kept: photo.jpg, diagram.png]")
    assert isinstance(replay[-1]["content"], str)


def test_ask_image_only_prompt(client, env):
    seen: list[httpx.Request] = []
    gateway(happy_stream(), seen)
    t = client.post("/api/ai/threads", json={}).json()
    r = client.post(f"/api/ai/threads/{t['id']}/messages", json={"content": "", "mode": "vision",
                                                                 "attachments": [image("receipt.jpg")]})
    assert r.status_code == 200
    body = json.loads(seen[0].content)
    assert body["model"] == "local/vision"
    assert body["messages"][-1]["content"] == [{"type": "image_url", "image_url": {"url": data_url(JPEG)}}]
    row = client.get("/api/ai/threads").json()[0]
    assert row["title"] == "receipt.jpg" and row["preview"] == "receipt.jpg"


@pytest.mark.parametrize("att,status,title", [
    (image(mime="image/gif"), 422, "That image couldn't be sent"),
    ({**image(), "image": "data:image/jpeg;base64,!!!notbase64"}, 422, "That image couldn't be sent"),
    (image(raw=b"GIF89a" + b"\x00" * 20), 422, "That image couldn't be sent"),          # mime says jpeg, bytes don't
    (image(raw=b"\xff\xd8\xff" + b"\x00" * (ai.MAX_IMAGE_BYTES + 1)), 413, "That image is too large"),
])
def test_ask_invalid_images_are_refused_before_anything_is_stored(client, env, att, status, title):
    seen: list[httpx.Request] = []
    gateway(happy_stream(), seen)
    t = client.post("/api/ai/threads", json={}).json()
    r = client.post(f"/api/ai/threads/{t['id']}/messages", json={"content": "look", "attachments": [att]})
    assert r.status_code == status and r.json()["error"]["title"] == title
    assert seen == [] and client.get(f"/api/ai/threads/{t['id']}").json()["messages"] == []


def test_ask_image_count_and_gateway_size_limits(client, env, monkeypatch):
    seen: list[httpx.Request] = []
    gateway(happy_stream(), seen)
    t = client.post("/api/ai/threads", json={}).json()
    r = client.post(f"/api/ai/threads/{t['id']}/messages",
                    json={"content": "five", "attachments": [image(f"{i}.jpg") for i in range(5)]})
    assert r.status_code == 422 and r.json()["error"]["title"] == "Up to 4 images per message"
    monkeypatch.setattr(ai, "GATEWAY_BODY_MAX", 1000)
    r = client.post(f"/api/ai/threads/{t['id']}/messages", json={"content": "big", "attachments": [image()] * 4})
    assert r.status_code == 413 and r.json()["error"]["title"] == "Too much to send in one message"
    assert seen == [] and client.get(f"/api/ai/threads/{t['id']}").json()["messages"] == []


def test_ask_image_gateway_refusals_are_human(client, env):
    t = client.post("/api/ai/threads", json={}).json()
    gateway(httpx.Response(422, json={"error": {"message": "local/fast does not accept images",
                                                "type": "not_supported", "code": "images_not_supported",
                                                "lif": {"use": "local/vision"}}}), [])
    evs = sse(client.post(f"/api/ai/threads/{t['id']}/messages",
                          json={"content": "what is this", "mode": "fast", "attachments": [image()]}).text)
    err = next(d for e, d in evs if e == "error")
    assert err["title"] == "Fast mode can't read images" and "Vision" in err["next_step"]

    # local/auto with images: the gateway sends them to local/vision, which isn't installed
    gateway(httpx.Response(503, json={"error": {"message": "no local model is deployed for local/vision; no vision "
                                                           "model is installed yet", "type": "capacity"}}), [])
    evs = sse(client.post(f"/api/ai/threads/{t['id']}/messages",
                          json={"content": "what is this", "mode": "auto", "attachments": [image()]}).text)
    err = next(d for e, d in evs if e == "error")
    assert err["title"] == "No local vision model is installed yet"
    assert {"label": "Find a vision model", "action": "/models/discovery?category=vision"} in err["actions"]
    assert "HTTP" not in json.dumps(err) or err["tech"]                   # raw text only in tech

    # installed but not answering: a different, honest message
    env["snap"].roles[2] = env["snap"].roles[2].model_copy(update={"cause": None, "state": "offline"})
    evs = sse(client.post(f"/api/ai/threads/{t['id']}/messages",
                          json={"content": "again", "mode": "vision", "attachments": [image()]}).text)
    assert next(d for e, d in evs if e == "error")["title"] == "The vision model isn't answering"


def test_vision_discovery_everywhere(client, env):
    disc = intent.resolve("Check for better vision models", env["snap"], ADMIN)
    assert disc.kind == "model_request" and disc.proposed_action.body == {"categories": ["vision"]}
    assert disc.navigate == "/models/discovery?category=vision"
    upstreams({})
    cat = client.get("/api/agents").json()["catalog"]
    scout = next(a for a in cat if a["id"] == "model-scout")
    assert "vision" in [o["value"] for o in scout["params"][0]["options"]]
