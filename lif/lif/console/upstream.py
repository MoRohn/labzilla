"""Allowlisted calls from the console BFF to in-cluster LIF services (spec §75–§77).

There is no generic passthrough: route modules call specific upstream paths through these helpers.
Keys stay server-side (controller and knowledge: LIF_CONSOLE_ADMIN_KEY, gateway: LIF_CONSOLE_GATEWAY_KEY
plus `X-LIF-Workload: console`) and are never logged or returned. Keys are read per request, so a
rotated secret is picked up without a restart. Failures raise UpstreamError, which `to_human()`
turns into a HumanError or which read paths turn into an honest "not available" state.

Decision Fabric is reached through the controller (/v1/de/*), knowledge through LIF_KNOWLEDGE_URL
when set. Prometheus helpers return [] when Prometheus can't answer (pass strict=True to get the error),
because every Prometheus-backed number is optional garnish on the console. Owner: SYS.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx

from lif.common import log
from lif.console import settings
from lif.console.errors import HumanHTTPError, human

LOG = log.get("lif.console.upstream")

Svc = Literal["controller", "gateway", "batch", "prometheus", "knowledge", "earn"]

SERVICE_LABEL: dict[str, str] = {"controller": "Model control service", "gateway": "AI gateway",
                                 "batch": "Batch service", "prometheus": "Metrics history",
                                 "knowledge": "Knowledge service", "earn": "Earning service"}
# What the user loses while each upstream is unreachable (said once, here, for every error message).
SERVICE_IMPACT: dict[str, str] = {
    "controller": "Model changes, discovery and history are unavailable; local AI keeps answering.",
    "gateway": "Local AI can't answer requests right now.",
    "batch": "Batch jobs can't be listed or controlled right now; queued work is kept.",
    "prometheus": "History and the memory breakdown are unavailable; live status still works.",
    "knowledge": "Knowledge search and decision records are unavailable.",
    "earn": "Earning status can't be read here; the service keeps enforcing its own limits and safety stops.",
}


class UpstreamError(Exception):
    """An upstream call failed. status=0 means it never answered (connect error or timeout)."""

    def __init__(self, svc: Svc, status: int, detail: str = ""):
        super().__init__(f"{svc}: {status} {detail}"[:300])
        self.svc, self.status, self.detail = svc, status, detail

    @property
    def unreachable(self) -> bool:
        return self.status == 0

    @property
    def unauthorized(self) -> bool:
        return self.status in (401, 403)


# ── clients ───────────────────────────────────────────────────────────────────────────────────

_transport: httpx.AsyncBaseTransport | None = None
_clients: dict[str, httpx.AsyncClient] = {}
_loop: asyncio.AbstractEventLoop | None = None


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Route every upstream call through `transport` (tests: httpx.MockTransport). Resets pooled clients."""
    global _transport
    _transport = transport
    _clients.clear()


def _base(svc: Svc) -> str:
    if svc == "controller":
        return settings.controller_url()
    if svc == "gateway":
        return settings.gateway_url()
    if svc == "batch":
        return settings.batch_url()
    if svc == "prometheus":
        return settings.prometheus_url()
    if svc == "earn":
        earn = settings.earn_url()
        if not earn:
            raise UpstreamError("earn", 0, "earning service not configured")
        return earn
    url = settings.knowledge_url()
    if not url:
        raise UpstreamError("knowledge", 0, "knowledge service not configured")
    return url


def _client(svc: Svc) -> httpx.AsyncClient:
    """One pooled client per service and event loop (a client must not outlive the loop it was used on)."""
    global _loop
    loop = asyncio.get_running_loop()
    if loop is not _loop:
        _clients.clear()        # old loop's clients are unusable; dropping them is enough (no I/O)
        _loop = loop
    c = _clients.get(svc)
    if c is None:
        # Idle connections expire before uvicorn's 5 s keep-alive does: with poll_sec 5 the server would otherwise
        # close a pooled connection just as the next poll reuses it, and that poll reports the service down.
        c = httpx.AsyncClient(transport=_transport, follow_redirects=False,
                              limits=httpx.Limits(max_connections=20, max_keepalive_connections=5, keepalive_expiry=2.0))
        _clients[svc] = c
    return c


def _headers(svc: Svc) -> dict[str, str]:
    h = {"Accept": "application/json"}
    key = settings.gateway_key() if svc == "gateway" else settings.admin_key() if svc in ("controller", "knowledge") else None
    if key:
        h["Authorization"] = f"Bearer {key}"
    if svc == "gateway":
        h["X-LIF-Workload"] = "console"
    if svc == "earn" and (earn_key := settings.earn_read_key()):
        h["X-Earn-Key"] = earn_key          # read-only: /api/status. The console sends nothing else to earn.
    return h


def _detail(resp: httpx.Response) -> str:
    """The upstream's own error message (LIF services use {error: str} or {error: {message}}), truncated."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300].strip()
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        err = err.get("message") or err.get("detail")
    if err is None and isinstance(body, dict):
        err = body.get("detail")
    return str(err if err is not None else body)[:300]


# Paths are built by route modules from ids that arrived in a URL. Routes validate their ids; this is the
# backstop that keeps the allowlist an allowlist: nothing may change which upstream path is called
# (dot segments, '//', a query or fragment, backslashes, whitespace, or their percent-encoded forms).
_UNSAFE_PATH = re.compile(r"(^|/)\.{1,2}(/|$)|//|[?#\\\s\x00-\x1f\x7f]|%(2f|2e|3f|23|5c|00)", re.I)


def _safe(svc: Svc, path: str) -> str:
    if not path.startswith("/") or _UNSAFE_PATH.search(path):
        log.event(LOG, "upstream_path_refused", svc=svc)
        raise UpstreamError(svc, 404, "refused by the console: unsafe upstream path")
    return path


async def _request(svc: Svc, method: str, path: str, *, params: dict[str, Any] | None = None,
                   json: dict[str, Any] | None = None, timeout: float) -> Any:
    url = _base(svc) + _safe(svc, path)
    tmo = httpx.Timeout(timeout, connect=min(timeout, 3.0))
    try:
        try:
            resp = await _client(svc).request(method, url, params=params, json=json, headers=_headers(svc), timeout=tmo)
        except (httpx.RemoteProtocolError, httpx.ReadError):
            # A pooled connection the server closed while idle fails before any response; a read is safe to repeat once
            # on a fresh connection. Mutations are never repeated (the first attempt may have been applied).
            if method != "GET":
                raise
            resp = await _client(svc).request(method, url, params=params, json=json, headers=_headers(svc), timeout=tmo)
    except httpx.TimeoutException:
        raise UpstreamError(svc, 0, "timed out") from None
    except httpx.HTTPError as e:
        raise UpstreamError(svc, 0, type(e).__name__) from None
    if resp.status_code >= 400:
        raise UpstreamError(svc, resp.status_code, _detail(resp))
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:      # an HTML error page from a proxy, or a non-JSON 200
        raise UpstreamError(svc, 502, f"not JSON: {resp.text[:120].strip()}") from None


async def get(svc: Svc, path: str, *, params: dict[str, Any] | None = None, timeout: float = 5.0) -> Any:
    """GET svc+path → parsed JSON."""
    return await _request(svc, "GET", path, params=params, timeout=timeout)


async def post(svc: Svc, path: str, json: dict[str, Any] | None = None, *, actor: str | None = None,
               timeout: float = 10.0) -> Any:
    """POST JSON → parsed JSON.

    No LIF upstream records a per-user actor today (the controller attributes to the key name, and
    model operations to 'operator'), so `actor` is logged with the call and the console's own
    auth.audit() is the authoritative record of who did it.
    """
    if actor:
        log.event(LOG, "upstream_mutation", svc=svc, path=path, actor=actor)
    return await _request(svc, "POST", path, json=json or {}, timeout=timeout)


async def delete(svc: Svc, path: str, *, actor: str | None = None, timeout: float = 10.0) -> Any:
    """DELETE → parsed JSON (controller DELETE /v1/models/{mid})."""
    if actor:
        log.event(LOG, "upstream_mutation", svc=svc, path=path, method="DELETE", actor=actor)
    return await _request(svc, "DELETE", path, timeout=timeout)


async def stream(svc: Svc, path: str, json: dict[str, Any], *, timeout: float = 300.0,
                 headers: dict[str, str] | None = None) -> AsyncIterator[str]:
    """POST and yield decoded text lines of a streaming (SSE) upstream response as they arrive.

    A non-2xx status raises UpstreamError before any line is yielded. Note the gateway can answer an
    upstream 4xx with HTTP 200 and raw JSON error lines, so callers still check line content.
    `headers` adds per-request headers (e.g. X-LIF-Data-Class) on top of the service's auth headers.
    """
    url = _base(svc) + _safe(svc, path)
    h = {**_headers(svc), "Accept": "text/event-stream", **(headers or {})}
    try:
        async with _client(svc).stream("POST", url, json=json, headers=h,
                                       timeout=httpx.Timeout(timeout, connect=3.0)) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                raise UpstreamError(svc, resp.status_code, _detail(resp))
            async for line in resp.aiter_lines():
                yield line
    except httpx.TimeoutException:
        raise UpstreamError(svc, 0, "timed out") from None
    except httpx.HTTPError as e:
        raise UpstreamError(svc, 0, type(e).__name__) from None


# ── Prometheus ────────────────────────────────────────────────────────────────────────────────

async def _prom(path: str, params: dict[str, Any], timeout: float, strict: bool) -> list[dict[str, Any]]:
    try:
        body = await get("prometheus", path, params=params, timeout=timeout)
        if not isinstance(body, dict) or body.get("status") not in (None, "success"):
            raise UpstreamError("prometheus", 502, str((body or {}).get("error") if isinstance(body, dict) else body)[:200])
        data = body.get("data")
        result = data.get("result") if isinstance(data, dict) else None
        return result if isinstance(result, list) else []
    except UpstreamError:
        if strict:
            raise
        return []


async def prom(query: str, *, timeout: float = 3.0, strict: bool = False) -> list[dict[str, Any]]:
    """Prometheus instant query → data.result ([{metric: {...}, value: [ts, "str"]}]); [] when unavailable."""
    return await _prom("/api/v1/query", {"query": query}, timeout, strict)


async def prom_range(query: str, start: float, end: float, step: float, *,
                     timeout: float = 5.0, strict: bool = False) -> list[dict[str, Any]]:
    """Prometheus range query → data.result ([{metric: {...}, values: [[ts, "str"], …]}]); [] when unavailable."""
    return await _prom("/api/v1/query_range", {"query": query, "start": start, "end": end, "step": step},
                       timeout, strict)


def prom_value(result: list[dict[str, Any]]) -> float | None:
    """First sample of an instant-query result as a float (None when empty or NaN)."""
    for r in result or []:
        v = r.get("value") if isinstance(r, dict) else None
        if isinstance(v, list) and len(v) == 2:
            try:
                f = float(v[1])
            except (TypeError, ValueError):
                continue
            if f == f:
                return f
    return None


# ── errors → humans ──────────────────────────────────────────────────────────────────────────

def to_human(e: UpstreamError, *, doing: str = "", not_found: str = "") -> HumanHTTPError:
    """UpstreamError → HumanHTTPError for a mutation or a read that has no cached fallback.

    `doing` completes "Couldn't …" ("start the benchmark"); `not_found` titles a 404. Upstream 409
    messages are the controller's own gate explanations (e.g. "GPU-tier benchmarks need a gpusched
    command-job window"), so they become the impact line; the raw detail always stays in tech.
    """
    name = SERVICE_LABEL.get(e.svc, e.svc)
    raw = {"service": e.svc, "status": e.status or "no answer", "detail": e.detail or None}
    raw = {k: v for k, v in raw.items() if v is not None}
    title = f"Couldn't {doing}" if doing else f"{name} didn't complete that"
    if e.unreachable:
        return human(503, f"{name} isn't answering", SERVICE_IMPACT.get(e.svc, ""),
                     "Retry in a moment. Nothing was changed.", [("Retry", "retry")], raw)
    if e.unauthorized:
        return human(503, f"The console isn't connected to the {name.lower()}",
                     "The console's access key is missing or was rejected, so this can't be done from here.",
                     "The owner needs to add the console key to the platform secrets and restart the service.",
                     [("View system status", "/system/services")], raw)
    if e.status == 404:
        return human(404, not_found or "Not found", "It may have been removed or renamed. Nothing was changed.",
                     "Refresh the list and try again.", [("Retry", "retry")], raw)
    if e.status in (400, 409, 422):
        impact = (e.detail or "The service refused it in its current state.").rstrip(".") + ". Nothing was changed."
        return human(409, title, impact, "Check the current state, then try again.", [("Retry", "retry")], raw)
    if e.status == 429:
        return human(429, f"{name} is busy", "It's limiting requests right now. Nothing was changed.",
                     "Wait a moment and try again.", [("Retry", "retry")], raw)
    return human(502, title, f"The {name.lower()} reported an error. Nothing was changed.",
                 "Retry in a moment.", [("Retry", "retry")], raw)


def reason(e: Exception) -> str:
    """One short sentence for a read path that degraded to cached or empty data."""
    if isinstance(e, UpstreamError):
        name = SERVICE_LABEL.get(e.svc, e.svc)
        if e.unreachable:
            return f"{name} isn't answering"
        if e.unauthorized:
            return f"The console isn't connected to the {name.lower()} (access key missing or rejected)"
        return f"{name} reported an error"
    return "Unexpected response from a Labzilla service"


async def close() -> None:
    """Close pooled HTTP clients (app shutdown)."""
    clients = list(_clients.values())
    _clients.clear()
    for c in clients:
        try:
            await c.aclose()
        except (RuntimeError, httpx.HTTPError):
            pass

