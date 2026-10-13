"""LIF gateway — the only application-facing AI endpoint.

OpenAI-compatible: /v1/chat/completions, /v1/completions, /v1/embeddings, /v1/models.
Platform: /v1/health, /v1/capabilities, /v1/batch (proxied to the batch engine), /metrics.

Every response says which physical model served it and whether that was a fallback:
  non-streaming → a top-level `lif` object in the JSON body
  streaming     → X-LIF-* response headers, plus `lif` in the final usage chunk
Fallback is never hidden.

Request headers (all optional):
  X-LIF-Data-Class   PUBLIC | INTERNAL | CONFIDENTIAL | RESTRICTED (default CONFIDENTIAL)
  X-LIF-Workload     free-form tag for accounting (e.g. "bnn-stories")
  X-LIF-Priority     2..8 (spec priority class; default 2 interactive)
  X-LIF-Timezone     IANA zone of the person asking (dates like "last night" resolve in it)
Body extension (optional): "lif": {"budget": {...}, "web": "auto" | "off" | "required", "tz": "<IANA>"}
  — see policy.Budget and docs/WEB_GROUNDING.md. local/auto defaults to web "auto"; other aliases to "off".
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections import OrderedDict, defaultdict
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from lif.common import config, log, metrics
from lif.decision.fabric import DecisionFabric
from lif.decision.providers import JevProvider
from lif.decision.rules import rules
from lif.decision.types import load_definitions
from lif.gpu import workload
from lif.gpu.state import BlerbzState, GpuStateWatcher
from lif.policy import engine as policy
from lif.routing.router import AUTO_ALIAS, ROUTE_TO_ALIAS, VISION_ALIAS, NoRoute, NotSupported, Route, Router
from lif.web import ground as webground
from lif.web.freshness import assess_turn
from lif.web.search import SearxngProvider
from lif.web.sports import SportsFeed
from lif.web.weather import WeatherFeed

LOG = log.get("lif.gateway")


# ── auth & rate limiting ──────────────────────────────────────────────────────

def load_keys() -> dict[str, str]:
    """key → client name, from `name:key` lines (Secret lif-gateway-keys)."""
    return config.keys("LIF_GATEWAY_KEYS")


class RateLimiter:
    def __init__(self, rpm: int):
        self.rate, self.cap = rpm / 60.0, float(rpm)
        self.buckets: dict[str, tuple[float, float]] = {}

    def allow(self, who: str) -> bool:
        now = time.monotonic()
        tokens, last = self.buckets.get(who, (self.cap, now))
        tokens = min(self.cap, tokens + (now - last) * self.rate)
        if tokens < 1:
            self.buckets[who] = (tokens, now)
            return False
        self.buckets[who] = (tokens - 1, now)
        return True


# ── Primary-workload bandwidth yield ──────────────────────────────────────────

class YieldLimiter:
    """Per-profile concurrency that shrinks while primary-workload production runs.
    GB10 CPU and GPU share one LPDDR5X bus; production LLM decode is bandwidth bound."""

    def __init__(self):
        self.active: dict[str, int] = defaultdict(int)
        self.cond = asyncio.Condition()

    def limit(self, profile_concurrency: int, state: BlerbzState) -> int:
        y = config.get("yield") or {}
        if state == BlerbzState.IMMINENT:
            return max(1, workload.yield_int("cpu_concurrency_busy", "cpu_concurrency_blerbz", 1))
        return max(1, min(profile_concurrency, int(y.get("cpu_concurrency_normal", 4))))

    @asynccontextmanager
    async def slot(self, profile: str, limit_fn, timeout: float, upstream_busy=None):
        """`upstream_busy` (async → int) is consulted only while the primary workload is IMMINENT: the
        model server's own in-flight count is shared truth across gateway replicas, so the
        production-time cap holds cluster-wide, not per replica."""
        deadline = time.monotonic() + timeout
        if upstream_busy is not None:
            while await upstream_busy() >= limit_fn():
                if time.monotonic() >= deadline:
                    raise TimeoutError("queue timeout waiting for a model slot (primary-workload yield)")
                metrics.throttled.labels("cluster_yield_wait").inc()
                await asyncio.sleep(0.25)
        async with self.cond:
            while self.active[profile] >= limit_fn():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("queue timeout waiting for a model slot")
                metrics.throttled.labels("queued").inc()
                try:
                    await asyncio.wait_for(self.cond.wait(), timeout=min(remaining, 1.0))
                except asyncio.TimeoutError:
                    pass
            self.active[profile] += 1
        metrics.inflight.labels(profile).inc()
        try:
            yield
        finally:
            metrics.inflight.labels(profile).dec()
            async with self.cond:
                self.active[profile] -= 1
                self.cond.notify_all()


async def _upstream_busy(endpoint: str) -> int:
    """llamacpp:requests_processing from the model server (0 if unreadable → fail-open is
    acceptable here: the per-replica cap still applies)."""
    try:
        r = await S.http.get(f"{endpoint}/metrics", timeout=2)
        for line in r.text.splitlines():
            if line.startswith("llamacpp:requests_processing"):
                return int(float(line.split()[-1]))
    except Exception:
        pass
    return 0


# ── deterministic tier: response cache for temperature-0 requests ─────────────

class ResponseCache:
    def __init__(self, size: int = 2000, ttl: float = 3600):
        self.size, self.ttl = size, ttl
        self.d: OrderedDict[str, tuple[float, dict]] = OrderedDict()

    @staticmethod
    def key(profile: str, revision: str, body: dict) -> str | None:
        # Only explicitly deterministic requests: non-streaming with temperature == 0.
        try:
            temp = float(body["temperature"]) if body.get("temperature") is not None else None
        except (TypeError, ValueError):
            return None                      # not deterministic as far as the cache can tell
        if body.get("stream") or temp != 0.0:
            return None
        clean = {k: v for k, v in body.items() if k not in ("lif", "user", "stream_options")}
        return hashlib.sha256(json.dumps([profile, revision, clean], sort_keys=True).encode()).hexdigest()

    def get(self, k: str | None) -> dict | None:
        if not k or k not in self.d:
            return None
        ts, v = self.d[k]
        if time.time() - ts > self.ttl:
            self.d.pop(k, None)
            return None
        self.d.move_to_end(k)
        return v

    def put(self, k: str | None, v: dict) -> None:
        if not k:
            return
        self.d[k] = (time.time(), v)
        while len(self.d) > self.size:
            self.d.popitem(last=False)


# ── app ──────────────────────────────────────────────────────────────────────

class State:
    def __init__(self):
        self.router = Router(controller_url=os.environ.get("LIF_CONTROLLER_URL"))
        self.gpu = GpuStateWatcher()
        self.keys = load_keys()
        self.ratelimit = RateLimiter(int(config.get("gateway.rate_limit_rpm", 600)))
        self.yielder = YieldLimiter()
        self.cache = ResponseCache()
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(float(config.get("gateway.request_timeout_sec", 300)),
                                                            connect=3))
        self.fabric = DecisionFabric(load_definitions(), rules, jev=JevProvider(config.secret("TYPE_SAFE_JEV_API_KEY")))
        self.batch_url = os.environ.get("LIF_BATCH_URL", "http://batch.ai-system.svc:8080")
        searx = os.environ.get("LIF_SEARXNG_URL") or config.get("web.searxng_url")
        self.grounder = webground.Grounder(SearxngProvider(searx) if searx else None, SportsFeed(),
                                           httpx.AsyncClient(timeout=3.0, follow_redirects=False), WeatherFeed())
        self.started = time.time()
        self.tasks: list[asyncio.Task] = []


S: State


async def _loop(fn, every: float):
    while True:
        try:
            await fn()
        except Exception:
            LOG.exception("background loop failed")
        await asyncio.sleep(every)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global S
    log.setup()
    S = State()
    await S.router.probe_all()
    await S.gpu.refresh()
    async def refresh():
        await S.router.refresh_table()
        if S.router.jev_enabled is not None:
            S.fabric.jev_enabled_override = S.router.jev_enabled
    S.tasks = [asyncio.create_task(_loop(S.router.probe_all, 10)),
               asyncio.create_task(_loop(refresh, 15)),
               asyncio.create_task(S.gpu.run())]
    if config.get("web.enabled", True) and S.grounder.sports is not None:
        # Team directories (~1 MB, refreshed daily) so the first sports question doesn't wait for them.
        async def prewarm():
            await asyncio.sleep(5)              # after startup probes; tests cancel the loops before this
            await _loop(S.grounder.sports.prewarm, 6 * 3600)
        S.tasks.append(asyncio.create_task(prewarm()))
    yield
    for t in S.tasks:
        t.cancel()
    await asyncio.gather(*S.tasks, return_exceptions=True)
    await S.http.aclose()


app = FastAPI(title="LIF gateway", lifespan=lifespan)
PUBLIC_PATHS = {"/v1/health", "/metrics", "/healthz", "/readyz"}


@app.middleware("http")
async def auth_mw(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    client = S.keys.get(key)
    if client is None:
        return JSONResponse({"error": {"message": "missing or invalid API key", "type": "auth"}}, status_code=401)
    if not S.ratelimit.allow(client):
        return JSONResponse({"error": {"message": "rate limit exceeded", "type": "rate_limit"}}, status_code=429)
    cl = int(request.headers.get("content-length") or 0)
    if cl > int(config.get("gateway.max_body_bytes", 4 << 20)):
        return JSONResponse({"error": {"message": "request body too large", "type": "invalid_request"}},
                            status_code=413)
    request.state.client = client
    return await call_next(request)


def _err(status: int, msg: str, typ: str = "invalid_request", **extra) -> JSONResponse:
    return JSONResponse({"error": {"message": msg, "type": typ, **extra}}, status_code=status)


# ── health / capabilities / models ───────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    # Ready = can serve at least one text alias with a verified-healthy model.
    ok = any(S.router.healthy(p) for p in S.router.profiles)
    return JSONResponse({"ready": ok}, status_code=200 if ok else 503)


@app.get("/v1/health")
async def health():
    snap = S.gpu.current()
    aliases = S.router.alias_status(_imminent())
    useful = aliases.get("local/fast", {}).get("available") or aliases.get("local/default", {}).get("available")
    return {"status": "ok" if useful else "degraded", "gateway": "ready", "useful_local_ai": bool(useful),
            "blerbz": {"state": snap.state.name, "reason": snap.reason},
            "decision_fabric": {"jev_enabled": S.fabric._jev_enabled(),
                                "jev_breaker_open": S.fabric.jev.breaker.open},
            "routing_table": S.router.table_source, "uptime_sec": round(time.time() - S.started)}


@app.get("/v1/capabilities")
async def capabilities():
    snap = S.gpu.current()
    return {"aliases": S.router.alias_status(_imminent()),
            "profiles": {n: {"endpoint_healthy": S.router.healthy(n), "params_b": p.params_b, "device": p.device,
                             "category": p.category, "context": p.context, "vision": p.vision,
                             "model": f"{p.hf_repo}@{p.revision}", "last_error": S.router.health[n].last_error}
                         for n, p in S.router.profiles.items()},
            "blerbz": snap.to_dict(),
            "yield": {"cpu_concurrency": S.yielder.limit(4, snap.state)},
            "decision_fabric": S.fabric.status()}


@app.get("/v1/models")
async def models():
    st = S.router.alias_status(_imminent())
    data = [{"id": a, "object": "model", "owned_by": "lif", "created": int(S.started),
             "lif": st.get(a, {})} for a in sorted(set(S.router.aliases) | {AUTO_ALIAS})]
    return {"object": "list", "data": data}


@app.get("/metrics")
async def prom():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── inference ────────────────────────────────────────────────────────────────

def _imminent() -> bool:
    return S.gpu.current().state == BlerbzState.IMMINENT


def _auth(route: Route) -> dict:
    if route.profile.auth_secret:
        return {"Authorization": f"Bearer {config.secret(route.profile.auth_secret) or ''}"}
    return {}


def _last_user(body: dict) -> str:
    for m in reversed(body.get("messages") or []):
        if m.get("role") == "user":
            c = m.get("content")
            return c if isinstance(c, str) else " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return body.get("prompt", "") if isinstance(body.get("prompt"), str) else ""


_IMAGE_PARTS = {"image_url", "input_image", "image"}


def _has_image(body: dict) -> bool:
    """Any message carries an image part (OpenAI `{"type": "image_url", "image_url": {"url": ...}}`)."""
    for m in body.get("messages") or []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") in _IMAGE_PARTS for p in c):
            return True
    return False


# ── live information (docs/WEB_GROUNDING.md) ─────────────────────────────────

_QUESTION = re.compile(r"(?i)\?\s*$|^\s*(?:who|what|when|where|which|why|how|is|are|was|were|did|does|do|will|can"
                       r"|has|have|should|could|would|tell me|any)\b")


class WebPlan:
    """What the gateway does about live information for one chat request."""

    def __init__(self, mode: str, text: str, history: list[str], now, decision: dict | None = None,
                 live: bool = False, kind: str = "general", allowed: bool = True):
        self.mode, self.text, self.history, self.now = mode, text, history, now
        self.decision, self.live, self.kind, self.allowed = decision, live, kind, allowed
        self.grounding: webground.Grounding | None = None
        self.route_result = None            # request-route, when it was decided together with needs-live-data

    @property
    def guard(self) -> bool:
        """Watch the answer's opening for a knowledge-cutoff disclaimer (only for questions not looked up)."""
        return (self.grounding is None and self.allowed and self.kind != "personal"
                and bool(config.get("web.disclaimer_guard", True)) and bool(_QUESTION.search(self.text)))


def _user_texts(body: dict) -> list[str]:
    out = []
    for m in body.get("messages") or []:
        if m.get("role") == "user":
            c = m.get("content")
            out.append(c if isinstance(c, str) else " ".join(p.get("text", "") for p in c or [] if isinstance(p, dict)))
    return out


async def _web_plan(body: dict, request: Request) -> WebPlan | None:
    """None: leave the request alone (non-chat, images, web off for this alias, or web disabled)."""
    if not config.get("web.enabled", True) or not body.get("messages") or _has_image(body):
        return None
    ext = body.get("lif") if isinstance(body.get("lif"), dict) else {}
    requested = body.get("model") or "local/default"
    default = "auto" if requested == AUTO_ALIAS and config.get("web.default_for_auto", True) else "off"
    mode = str(ext.get("web") or request.headers.get("x-lif-web") or default).lower()
    if mode not in ("auto", "required"):
        return None
    users = _user_texts(body)
    if not users or not users[-1].strip():
        return None
    text, history = users[-1], users[:-1]
    now = webground.now_in(ext.get("tz") or request.headers.get("x-lif-timezone"))
    declared = request.headers.get("x-lif-data-class")
    allowed = policy.may_send(policy.classify(text, declared).data_class, "web_search")
    plan = WebPlan(mode, text, history, now, allowed=allowed)
    if mode == "required":
        f = assess_turn(text, history)
        plan.live, plan.kind = True, f.kind if f.kind not in ("none", "clock") else "general"
        plan.decision = {"decision": "web", "confidence": 1.0, "provider": "caller", "action": "required"}
        return plan
    state = {"last_user": text[:2000], "previous_user": (history[-1] if history else "")[:500]}
    if requested == AUTO_ALIAS and policy.DataClass.parse(declared, policy.DataClass.CONFIDENTIAL) == policy.DataClass.PUBLIC:
        # PUBLIC: one Jev request answers both questions (needs-live-data and request-route).
        both = await S.fabric.evaluate_group(["needs-live-data", "request-route"],
                                             {**state, **_route_state(body, text)}, data_class=declared)
        d, plan.route_result = both["needs-live-data"], both["request-route"]
    else:
        # Explicit aliases: rules only. Their receipt never shows a route decision, so the prompt must not
        # reach Jev for this one either.
        d = await S.fabric.evaluate("needs-live-data", state,
                                    data_class=declared if requested == AUTO_ALIAS else "CONFIDENTIAL")
    f = assess_turn(text, history)
    # The rules stay in force whoever answered: a confident rules "yes" is never overruled to "no".
    rules_yes = f.live and f.confidence >= 0.8
    plan.live = (d.decision == "yes" and d.provider != "default") or rules_yes
    plan.kind = f.kind if f.kind not in ("none", "clock") else "general"
    if rules_yes and d.decision != "yes":
        d.decision, d.provider, d.confidence, d.action = "yes", "rules", f.confidence, policy.Gate.VALIDATE
    plan.decision = {"decision": "web" if plan.live else "no_web", "confidence": round(d.confidence, 3),
                     "provider": d.provider, "action": d.action}
    return plan


def _route_state(body: dict, text: str) -> dict:
    return {"last_user": text[:6000], "prompt_tokens": len(text) // 4,
            "structured_output": bool(body.get("response_format"))}


async def _rewrite_followup(text: str, convo: list[dict]) -> str | None:
    """A short follow-up ("what about Clemson?") → one standalone search query, by local/instant.
    Skipped while the primary workload is IMMINENT; the heuristic query is used instead."""
    if _imminent():
        return None
    try:
        route = S.router.resolve("local/instant")
    except NoRoute:
        return None
    lines = []
    for m in convo[-5:-1]:
        c = m.get("content")
        c = c if isinstance(c, str) else " ".join(p.get("text", "") for p in c or [] if isinstance(p, dict))
        lines.append(f"{m.get('role', 'user').title()}: {c[:400]}")
    prompt = "\n".join(lines) + f"\nUser (last message): {text[:400]}"
    body = {"model": route.profile.name, "max_tokens": 40, "temperature": 0, "stream": False,
            "messages": [{"role": "system", "content": "Rewrite the user's last message as one standalone web "
                          "search query. Resolve pronouns and references from the conversation. Keep names, "
                          "teams, places and dates. Output only the query."},
                         {"role": "user", "content": prompt}]}
    if route.profile.chat_template_kwargs:
        body["chat_template_kwargs"] = route.profile.chat_template_kwargs
    r = await S.http.post(f"{route.profile.endpoint}/v1/chat/completions", json=body, headers=_auth(route),
                          timeout=float(config.get("web.rewrite_timeout_sec", 3.0)))
    r.raise_for_status()
    out = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return re.sub(r"(?s)<think>.*?</think>", "", out).strip()


async def _ground(plan: WebPlan, body: dict) -> webground.Grounding:
    if not plan.allowed:
        g = webground.Grounding("blocked", kind=plan.kind,
                                note="this prompt's data class may not be looked up on the web")
    else:
        g = await S.grounder.ground(plan.text, plan.history, plan.now, plan.kind, rewrite=_rewrite_followup,
                                    convo=body.get("messages") or [])
    metrics.web_lookups.labels(plan.kind, g.status).inc()
    metrics.web_latency.labels("total").observe(g.ms / 1000)
    for k, v in g.timings.items():
        metrics.web_latency.labels(k).observe(v / 1000)
    plan.grounding = g
    return g


def _web_messages(plan: WebPlan, messages: list[dict]) -> list[dict]:
    g = plan.grounding
    msgs = webground.inject_date(messages, plan.now)
    if g is None:
        return msgs
    if g.status == "ok":
        return webground.inject_evidence(msgs, g, plan.now)
    return webground.inject_unavailable(msgs, g, plan.now)


def _web_route(requested: str, decision: dict | None) -> Route:
    """Grounded local/auto answers go to the web alias (the strongest local model for reading sources),
    else local/default: never the 1.7B instant tier."""
    rd = {**(decision or {}), "decision": "web"}
    for alias in (config.get("web.alias", "local/web"), "local/default"):
        try:
            return S.router.resolve(alias, requested=requested, blerbz_imminent=_imminent(), route_decision=rd)
        except NoRoute:
            continue
    raise NoRoute(requested, "no local model is available for a grounded answer")


async def _route(body: dict, request: Request, plan: WebPlan | None = None) -> Route:
    requested = body.get("model") or "local/default"
    vision = _has_image(body)
    if requested != AUTO_ALIAS:
        return S.router.resolve(requested, blerbz_imminent=_imminent(), need_vision=vision)
    if plan is not None and plan.live:
        return _web_route(requested, plan.decision)
    if vision:
        # Deterministic: images can only go to a model with an image projector. Never ask the
        # route decision (and never send image-bearing prompts to Jev).
        return S.router.resolve(VISION_ALIAS, requested=AUTO_ALIAS, blerbz_imminent=_imminent(), need_vision=True,
                                route_decision={"decision": "vision", "confidence": 1.0, "provider": "rules",
                                                "action": "auto", "why": "request contains images"})
    text = _last_user(body if plan is None else {"messages": [{"role": "user", "content": plan.text}]})
    d = plan.route_result if plan is not None and plan.route_result is not None else await S.fabric.evaluate(
        "request-route", _route_state(body, text), data_class=request.headers.get("x-lif-data-class"))
    choice = d.decision if d.action in (policy.Gate.AUTO, policy.Gate.VALIDATE) else "default"
    return S.router.resolve(ROUTE_TO_ALIAS[choice], requested=AUTO_ALIAS, blerbz_imminent=_imminent(),
                            route_decision={"decision": d.decision, "confidence": round(d.confidence, 3),
                                            "provider": d.provider, "action": d.action})


def _prepare(body: dict, route: Route, state: BlerbzState) -> dict:
    out = {k: v for k, v in body.items() if k != "lif"}
    out["model"] = route.profile.name
    if route.profile.runtime != "llama.cpp":
        out.pop("return_progress", None)          # llama-server's prompt-progress chunks; others may reject it
    if route.profile.chat_template_kwargs and "chat_template_kwargs" not in out:
        out["chat_template_kwargs"] = route.profile.chat_template_kwargs
    if state == BlerbzState.IMMINENT:
        cap = workload.yield_int("max_tokens_busy", "max_tokens_blerbz", 512)
        mt = out.get("max_tokens") or out.get("max_completion_tokens")
        if mt is None or int(mt) > cap:
            out["max_tokens"] = cap
            out.pop("max_completion_tokens", None)
            metrics.throttled.labels("max_tokens_clamped").inc()
    if out.get("stream"):
        out["stream_options"] = {**(out.get("stream_options") or {}), "include_usage": True}
    return out


def _tier(route: Route) -> str:
    return "large_local" if route.profile.params_b >= metrics.HEAVY_PARAMS_B else "fast_local"


def _account(route: Route, usage: dict, workload: str, seconds: float) -> None:
    pin, pout = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    metrics.tokens.labels(route.profile.name, "input").inc(pin)
    metrics.tokens.labels(route.profile.name, "output").inc(pout)
    metrics.tasks.labels(workload, _tier(route)).inc()
    c = config.get("cost.external_equivalent_usd_per_mtok") or {}
    metrics.cost_avoided.labels(_tier(route)).inc(pin * float(c.get("input", 0)) / 1e6 +
                                                  pout * float(c.get("output", 0)) / 1e6)
    if route.fallback:
        metrics.fallbacks.labels(route.requested, route.profile.name,
                                 (route.reason or "fallback").split(":")[0][:40]).inc()


async def _proxy(request: Request, path: str, endpoint_name: str):
    try:
        body = await request.json()
    except Exception:
        return _err(400, "body must be JSON")
    workload = request.headers.get("x-lif-workload", getattr(request.state, "client", "unknown"))
    requested = body.get("model") or "local/default"
    t0 = time.perf_counter()
    snap = S.gpu.current()
    vision = _has_image(body)
    plan, original_messages = None, None
    if path == "/v1/chat/completions" and isinstance(body, dict):
        try:
            plan = await _web_plan(body, request)
            if plan is not None:
                if plan.live:
                    await _ground(plan, body)
                original_messages = body.get("messages")
                body = {**body, "messages": _web_messages(plan, body["messages"])}
        except Exception as exc:        # live lookups are an enrichment: never fail the request for them
            LOG.exception("web plan failed; answering without a lookup",
                          extra={"fields": {"err": type(exc).__name__}})
            metrics.web_lookups.labels("unknown", "error").inc()
            plan, original_messages = None, None
    try:
        route = await _route(body, request, plan)
    except NotSupported as e:
        metrics.requests.labels(endpoint_name, requested, "422").inc()
        return _err(422, e.reason, "not_supported", code="images_not_supported",
                    lif={"requested": requested, "use": VISION_ALIAS})
    except NoRoute as e:
        metrics.requests.labels(endpoint_name, requested, "503").inc()
        metrics.tasks.labels(workload, "rejected").inc()
        return _err(503, e.reason, "capacity", lif={"requested": requested, "blerbz": snap.state.name})

    budget = policy.Budget.from_request((body.get("lif") or {}).get("budget"))
    grounded = plan is not None and plan.grounding is not None
    if (budget.max_latency_ms and snap.state == BlerbzState.IMMINENT and route.profile.params_b >= 4 and not vision
            and not grounded):
        # production is running and the caller is latency-sensitive: prefer the smaller model
        try:
            alt = S.router.resolve("local/instant", requested=route.requested, blerbz_imminent=True)
            alt.reason = "latency budget during primary-workload production"
            alt.fallback = True        # a deliberate downgrade, even when local/auto chose the original
            route = alt
        except NoRoute:
            pass

    exclude: set[str] = set()
    while True:
        upstream = _prepare(body, route, snap.state)
        # Live answers are never cached: a score or a price an hour old is a wrong answer.
        ck = (S.cache.key(route.profile.name, route.profile.revision, upstream)
              if path != "/v1/embeddings" and not grounded else None)
        hit = S.cache.get(ck)
        if hit is not None:
            metrics.tasks.labels(workload, "deterministic").inc()
            metrics.requests.labels(endpoint_name, requested, "200").inc()
            return JSONResponse({**hit, "lif": {**route.meta(), "cache": "hit"}})
        limit = lambda: S.yielder.limit(route.profile.concurrency, S.gpu.current().state)
        ep = route.profile.endpoint
        busy = (lambda: _upstream_busy(ep)) if (S.gpu.current().state == BlerbzState.IMMINENT
                                                and route.profile.device == "cpu") else None
        try:
            if upstream.get("stream"):
                return await _stream(route, path, upstream, limit, workload, endpoint_name, requested, t0, busy,
                                     plan=plan, original_messages=original_messages, raw_body=body)
            async with S.yielder.slot(route.profile.name, limit, timeout=120, upstream_busy=busy):
                r = await S.http.post(f"{route.profile.endpoint}{path}", json=upstream, headers=_auth(route))
            if r.status_code >= 500:
                raise httpx.HTTPStatusError(f"upstream {r.status_code}", request=r.request, response=r)
        except TimeoutError as e:
            metrics.requests.labels(endpoint_name, requested, "429").inc()
            return _err(429, str(e), "capacity", lif=route.meta())
        except (httpx.HTTPError, httpx.HTTPStatusError) as e:
            S.router.mark_failure(route.profile.name, str(e))
            exclude.add(route.profile.name)
            try:
                nxt = S.router.resolve(route.alias, requested=route.requested, exclude=exclude,
                                       blerbz_imminent=_imminent(), need_vision=vision)
                nxt.fallback, nxt.reason = True, f"upstream_error on {route.profile.name}: {str(e)[:120]}"
                route = nxt
                continue
            except NoRoute as nr:
                metrics.requests.labels(endpoint_name, requested, "503").inc()
                return _err(503, nr.reason, "capacity", lif={"requested": requested})
        break

    S.router.mark_success(route.profile.name)
    if r.status_code != 200:
        metrics.requests.labels(endpoint_name, requested, str(r.status_code)).inc()
        return Response(r.content, status_code=r.status_code, media_type="application/json")
    data = r.json()
    dt = time.perf_counter() - t0
    usage = data.get("usage") or {}
    _account(route, usage, workload, dt)
    tm = data.pop("timings", None) or {}
    if tm.get("predicted_per_second"):
        metrics.decode_tps.labels(route.profile.name).observe(tm["predicted_per_second"])
    if plan is not None and plan.guard and webground.is_disclaimer(_content(data)):
        regen = await _regenerate(plan, body, original_messages, path, workload)
        if regen is not None:
            route, data = regen
            ck = None
            dt = time.perf_counter() - t0
    data["model"] = route.requested
    S.cache.put(ck, data)
    data["lif"] = {**route.meta(), "blerbz": snap.state.name, "latency_ms": round(dt * 1000, 1)}
    if plan is not None:
        data["lif"]["web"] = _web_meta(plan)
    metrics.requests.labels(endpoint_name, requested, "200").inc()
    metrics.request_latency.labels(endpoint_name, requested).observe(dt)
    return JSONResponse(data)


def _content(data: dict) -> str:
    try:
        return str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    except (AttributeError, IndexError, TypeError):
        return ""


def _web_meta(plan: WebPlan) -> dict:
    g = plan.grounding
    base = {"decision": plan.decision}
    return {**base, **g.meta()} if g is not None else {**base, "status": "not_needed"}


def _plan_route(requested: str, plan: WebPlan) -> Route:
    if requested == AUTO_ALIAS:
        return _web_route(requested, plan.decision)
    return S.router.resolve(requested, blerbz_imminent=_imminent())


async def _guard_ground(plan: WebPlan, raw_body: dict, original_messages: list[dict]) -> dict:
    """The model opened with a knowledge-cutoff disclaimer: look the question up and build the grounded body."""
    metrics.web_guard.labels("caught").inc()
    plan.decision = {"decision": "web", "confidence": 1.0, "provider": "guard", "action": "regenerate",
                     "why": "the first answer opened with a knowledge-cutoff disclaimer"}
    if plan.grounding is None:
        f = assess_turn(plan.text, plan.history)
        plan.kind = f.kind if f.kind not in ("none", "clock") else "general"
        await _ground(plan, {**raw_body, "messages": original_messages})
    return {**raw_body, "messages": _web_messages(plan, original_messages)}


async def _regenerate(plan: WebPlan, raw_body: dict, original_messages: list[dict], path: str,
                      workload: str) -> tuple[Route, dict] | None:
    body = await _guard_ground(plan, raw_body, original_messages)
    try:
        route = _plan_route(raw_body.get("model") or "local/default", plan)
    except NoRoute:
        return None
    upstream = {**_prepare(body, route, S.gpu.current().state), "stream": False}
    upstream.pop("stream_options", None)
    limit = lambda: S.yielder.limit(route.profile.concurrency, S.gpu.current().state)
    try:
        async with S.yielder.slot(route.profile.name, limit, timeout=120):
            r = await S.http.post(f"{route.profile.endpoint}{path}", json=upstream, headers=_auth(route))
    except (TimeoutError, httpx.HTTPError):
        metrics.web_guard.labels("failed").inc()
        return None
    if r.status_code != 200:
        metrics.web_guard.labels("failed").inc()
        return None
    data = r.json()
    data.pop("timings", None)
    _account(route, data.get("usage") or {}, workload, 0.0)
    metrics.web_guard.labels("regenerated").inc()
    return route, data


async def _open_stream(route: Route, path: str, upstream: dict, limit, busy=None):
    """Hold a slot and open the upstream stream; on failure the slot is released and the error raised."""
    cm = S.yielder.slot(route.profile.name, limit, timeout=120, upstream_busy=busy)
    await cm.__aenter__()
    try:
        req = S.http.build_request("POST", f"{route.profile.endpoint}{path}", json=upstream, headers=_auth(route))
        resp = await S.http.send(req, stream=True)
        if resp.status_code >= 500:
            await resp.aclose()
            raise httpx.HTTPStatusError(f"upstream {resp.status_code}", request=req, response=resp)
    except BaseException:
        await cm.__aexit__(None, None, None)
        raise
    return cm, resp


_SENTENCE_END = re.compile(r"[.!?](?:\s|$)|\n")


def _opening_done(text: str, window: int) -> bool:
    """Enough of the answer to judge its opening: the first sentence (≥ 30 chars) or `window` characters."""
    return len(text) >= window or (len(text) >= 30 and bool(_SENTENCE_END.search(text, 25)))


async def _stream(route: Route, path: str, upstream: dict, limit, workload: str, endpoint_name: str,
                  requested: str, t0: float, busy=None, plan: WebPlan | None = None,
                  original_messages: list[dict] | None = None, raw_body: dict | None = None) -> StreamingResponse:
    # Acquire the slot and open the upstream BEFORE returning, so connection errors can
    # still fall back; once bytes flow to the client the route is committed.
    cm, resp = await _open_stream(route, path, upstream, limit, busy)
    window = int(config.get("web.disclaimer_window_chars", 220))

    async def gen() -> AsyncIterator[bytes]:
        nonlocal route
        first = True
        usage: dict = {}
        cur_cm, cur_resp = cm, resp
        # Disclaimer guard: hold the answer's opening until it can be judged (≈ one sentence). A
        # "my knowledge has a cutoff" opening is dropped and the question answered again with a lookup.
        guard = bool(plan is not None and plan.guard and original_messages)
        held: list[bytes] = []
        held_text = ""
        try:
            while True:
                switch = False
                try:
                    async for line in cur_resp.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("data: ") and line != "data: [DONE]":
                            try:
                                chunk = json.loads(line[6:])
                            except json.JSONDecodeError:
                                yield (line + "\n\n").encode()
                                continue
                            choices = chunk.get("choices") or []
                            text = "".join(str((c.get("delta") or {}).get("content") or c.get("text") or "")
                                           for c in choices)
                            if first and text:
                                metrics.ttft.labels(route.requested, route.profile.name).observe(time.perf_counter() - t0)
                                first = False
                            chunk["model"] = route.requested
                            chunk.pop("timings", None)
                            if chunk.get("usage"):
                                usage = chunk["usage"]
                                chunk["lif"] = {**route.meta(), **({"web": _web_meta(plan)} if plan else {})}
                            out = f"data: {json.dumps(chunk)}\n\n".encode()
                            if guard and (text or held):
                                held.append(out)
                                held_text += text
                                ended = any(c.get("finish_reason") for c in choices) or bool(chunk.get("usage"))
                                if not (ended or _opening_done(held_text, window)):
                                    continue
                                guard = False
                                if webground.is_disclaimer(held_text):
                                    switch = True
                                    break
                                for h in held:
                                    yield h
                                held = []
                                continue
                            yield out
                        else:
                            if held and line == "data: [DONE]":
                                guard = False
                                if webground.is_disclaimer(held_text):
                                    switch = True
                                    break
                                for h in held:
                                    yield h
                                held = []
                            yield (line + "\n\n").encode()
                finally:
                    await cur_resp.aclose()
                    await cur_cm.__aexit__(None, None, None)
                if not switch:
                    if held:                 # the stream ended without [DONE] while the opening was held
                        for h in held:
                            yield h
                    break
                got = await _regen_stream(plan, raw_body or {}, original_messages or [], path)
                if got is None:              # keep the original answer rather than none
                    for h in held:
                        yield h
                    yield b"data: [DONE]\n\n"
                    break
                route, cur_cm, cur_resp = got
                held, held_text, first, usage = [], "", True, {}
            S.router.mark_success(route.profile.name)
            metrics.requests.labels(endpoint_name, requested, "200").inc()
        except httpx.HTTPError as e:
            S.router.mark_failure(route.profile.name, str(e))
            metrics.requests.labels(endpoint_name, requested, "502").inc()
            yield f"data: {json.dumps({'error': {'message': 'upstream stream failed', 'type': 'upstream'}})}\n\n".encode()
        finally:
            dt = time.perf_counter() - t0
            _account(route, usage, workload, dt)
            metrics.request_latency.labels(endpoint_name, requested).observe(dt)

    headers = {"X-LIF-Served-By": route.profile.name, "X-LIF-Fallback": str(route.fallback).lower(),
               "X-LIF-Degraded": str(route.degraded).lower(), "X-LIF-Requested": route.requested}
    if route.reason:
        headers["X-LIF-Reason"] = route.reason[:200]
    if plan is not None:
        g = plan.grounding
        headers["X-LIF-Web"] = g.status if g else "not_needed"
        if g is not None:
            headers["X-LIF-Web-Sources"] = str(len(g.sources))
            if g.query:
                headers["X-LIF-Web-Query"] = quote(g.query[:200], safe=" ,:'-")
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


async def _regen_stream(plan: WebPlan | None, raw_body: dict, original_messages: list[dict], path: str):
    if plan is None:
        return None
    try:
        body = await _guard_ground(plan, raw_body, original_messages)
        route = _plan_route(raw_body.get("model") or "local/default", plan)
        upstream = _prepare(body, route, S.gpu.current().state)
        limit = lambda: S.yielder.limit(route.profile.concurrency, S.gpu.current().state)
        ep = route.profile.endpoint
        busy = (lambda: _upstream_busy(ep)) if _imminent() and route.profile.device == "cpu" else None
        cm, resp = await _open_stream(route, path, upstream, limit, busy)
    except (NoRoute, TimeoutError, httpx.HTTPError):
        metrics.web_guard.labels("failed").inc()
        return None
    metrics.web_guard.labels("regenerated").inc()
    return route, cm, resp


@app.post("/v1/chat/completions")
async def chat(request: Request):
    return await _proxy(request, "/v1/chat/completions", "chat")


@app.post("/v1/completions")
async def completions(request: Request):
    return await _proxy(request, "/v1/completions", "completions")


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    return await _proxy(request, "/v1/embeddings", "embeddings")


# ── batch (thin proxy; the batch engine owns durability) ─────────────────────

async def _batch_proxy(request: Request, method: str, path: str):
    try:
        content = await request.body()
        r = await S.http.request(method, f"{S.batch_url}{path}", content=content,
                                 params=request.query_params,
                                 headers={"content-type": "application/json",
                                          "x-lif-client": getattr(request.state, "client", "unknown"),
                                          "x-lif-data-class": request.headers.get("x-lif-data-class", "")},
                                 timeout=15)
        return Response(r.content, status_code=r.status_code, media_type="application/json")
    except httpx.HTTPError as e:
        return _err(503, f"batch engine unavailable: {e}", "capacity")


@app.post("/v1/batch")
async def batch_create(request: Request):
    return await _batch_proxy(request, "POST", "/v1/batch")


@app.get("/v1/batch")
async def batch_list(request: Request):
    return await _batch_proxy(request, "GET", "/v1/batch")


@app.get("/v1/batch/{bid}")
async def batch_get(bid: str, request: Request):
    return await _batch_proxy(request, "GET", f"/v1/batch/{quote(bid, safe='')}")


@app.get("/v1/batch/{bid}/results")
async def batch_results(bid: str, request: Request):
    return await _batch_proxy(request, "GET", f"/v1/batch/{quote(bid, safe='')}/results")


@app.delete("/v1/batch/{bid}")
async def batch_cancel(bid: str, request: Request):
    return await _batch_proxy(request, "DELETE", f"/v1/batch/{quote(bid, safe='')}")
