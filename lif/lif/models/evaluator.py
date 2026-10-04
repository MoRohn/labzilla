"""Evaluation harness: quality + performance against any OpenAI-compatible endpoint.

Quality   pass rate over an eval suite (evals/*.yaml), per category, JSON validity.
Latency   TTFT (streaming), decode tok/s (server timings), end-to-end p50/p95.
Load      throughput at `concurrency` parallel requests; errors and timeouts.

Every number in a summary is measured on this host; nothing is copied from model cards.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import struct
import time
import zlib
from pathlib import Path
from typing import Any

import httpx
import yaml

from lif.common import config, log

LOG = log.get("lif.eval")


def load_suite(name: str = "core") -> dict:
    for d in (Path("/etc/lif/evals"), config.REPO_CONFIG.parent / "evals"):
        p = d / f"{name}.yaml"
        if p.exists():
            return yaml.safe_load(p.read_text())
    raise FileNotFoundError(f"eval suite {name}")


# ── synthetic images (stdlib only: the controller image has no Pillow) ──────

# 5×7 block glyphs for digits, drawn at a large scale so any vision encoder can read them.
_GLYPHS = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
}
_COLORS = {"red": (220, 20, 20), "green": (20, 170, 40), "blue": (25, 60, 220), "yellow": (240, 210, 20),
           "black": (0, 0, 0), "white": (255, 255, 255)}


def png(width: int, height: int, pixel) -> bytes:
    """Encode an 8-bit RGB PNG. `pixel(x, y)` → (r, g, b)."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)                                   # filter type 0 (None) per scanline
        for x in range(width):
            raw.extend(pixel(x, y))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def render_image(spec: dict) -> bytes:
    """Declarative test images (evals/vision.yaml). Sizes default to 256 px: vision encoders have
    minimum pixel counts, and tiny images make answers unreliable."""
    kind, size = spec["kind"], int(spec.get("size", 256))
    col = lambda name: _COLORS[name]
    if kind == "solid":
        c = col(spec["color"])
        return png(size, size, lambda x, y: c)
    if kind == "split":          # left half / right half
        a, b = col(spec["left"]), col(spec["right"])
        return png(size, size, lambda x, y: a if x < size // 2 else b)
    if kind == "digit":          # one large digit, ink on paper
        g, ink, bg = _GLYPHS[str(spec["digit"])], col(spec.get("ink", "black")), col(spec.get("bg", "white"))
        cell = size // 9                                 # 5×7 glyph centred with a 2-cell / 1-cell margin
        ox, oy = (size - 5 * cell) // 2, (size - 7 * cell) // 2

        def px(x, y):
            gx, gy = (x - ox) // cell, (y - oy) // cell
            return ink if 0 <= gx < 5 and 0 <= gy < 7 and x >= ox and y >= oy and g[gy][gx] == "1" else bg
        return png(size, size, px)
    if kind == "squares":        # n separated solid squares in a row, on white
        n, c, bg = int(spec["count"]), col(spec.get("color", "blue")), col("white")
        side = size // (2 * n + 1)

        def px(x, y):
            k = x // side
            return c if k % 2 == 1 and k < 2 * n and side <= y < 2 * side else bg
        return png(size, 3 * side, px)
    raise ValueError(f"unknown image kind {kind}")


def data_uri(spec: dict) -> str:
    return "data:image/png;base64," + base64.b64encode(render_image(spec)).decode()


def item_messages(item: dict) -> list[dict]:
    """The chat messages for an item. A `grounded` item is built with the gateway's own prompt functions
    (lif.web.ground), so the web suite measures exactly what production sends: the date in the system
    message, the evidence block and grounding rules on the user turn."""
    g = item.get("grounded")
    if not g:
        return [{"role": "user", "content": item_content(item)}]
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from lif.web import ground as wg
    now = datetime.fromisoformat(g["now"]).replace(tzinfo=ZoneInfo(g.get("tz", "UTC")))
    msgs = wg.inject_date([{"role": "user", "content": item["prompt"]}], now)
    ev = [wg.Evidence(e.get("title", ""), e.get("url", ""), e["text"], e.get("kind", "snippet"), e.get("published"))
          for e in g.get("evidence") or []]
    if not ev:
        return wg.inject_unavailable(msgs, wg.Grounding("empty", note="the search returned nothing relevant"), now)
    return wg.inject_evidence(msgs, wg.Grounding("ok", evidence=ev), now)


def item_content(item: dict) -> str | list[dict]:
    """OpenAI chat content for an eval item: plain text, or text + image_url parts."""
    images = item.get("images") or ([item["image"]] if item.get("image") else [])
    if not images:
        return item["prompt"]
    return [*({"type": "image_url", "image_url": {"url": data_uri(i)}} for i in images),
            {"type": "text", "text": item["prompt"]}]


def _strip(text: str) -> str:
    text = re.sub(r"<think>[\s\S]*?</think>", "", text or "").strip()
    m = re.search(r"```(?:json|python)?\s*([\s\S]*?)```", text)
    return m.group(1).strip() if m and not text.startswith("def ") else text


_HEDGE = re.compile(r"(?i)hypothetical|fictional|knowledge cut-?off|as of my (?:last|latest)|future (?:date|timeline|event)"
                    r"|has not (?:yet )?(?:occurred|happened)|cannot (?:verify|confirm) (?:the )?date")


def check(item: dict, output: str) -> tuple[bool, dict]:
    """Deterministic checkers. Returns (passed, details)."""
    out = _strip(output)
    c = item["checker"]
    if c == "contains":
        missing = [e for e in item["expect"] if e.lower() not in out.lower()]
        return not missing, {"missing": missing}
    if c == "regex":
        return bool(re.search(item["expect"], out.strip())), {}
    if c == "choice":
        word = re.sub(r"[^a-z_ -]", "", out.lower()).strip().split()
        got = word[0] if word else ""
        return got == item["expect"], {"got": got}
    if c == "number":
        nums = re.findall(r"-?\d+(?:\.\d+)?", out)
        return bool(nums) and abs(float(nums[-1]) - float(item["expect"])) <= float(item.get("tol", 0)), \
            {"got": nums[-1] if nums else None}
    if c == "grounded":
        # every `expect` regex, an inline [n] citation (unless `cite: false`), and no cutoff disclaimer or hedging
        from lif.web.ground import is_disclaimer
        missing = [e for e in item.get("expect") or [] if not re.search(e, out)]
        cited = bool(re.search(r"\[\d+\]", out)) or item.get("cite") is False
        hedged = is_disclaimer(out) or bool(_HEDGE.search(out))
        forbidden = [e for e in item.get("forbid") or [] if re.search(e, out)]
        return not missing and cited and not hedged and not forbidden, \
            {"missing": missing, "cited": cited, "hedged": hedged, "forbidden": forbidden}
    if c == "json":
        try:
            obj = json.loads(out[out.find("{"): out.rfind("}") + 1])
        except Exception:
            return False, {"json_valid": False}
        ok = all(k in obj for k in item.get("required", []))
        for k, t in (item.get("types") or {}).items():
            ok &= isinstance(obj.get(k), {"int": int, "list": list, "str": str}[t])
        for k, allowed in (item.get("enums") or {}).items():
            ok &= obj.get(k) in allowed
        return bool(ok), {"json_valid": True}
    raise ValueError(f"unknown checker {c}")


async def _one(client: httpx.AsyncClient, url: str, model: str, prompt: str | list, max_tokens: int,
               extra: dict, messages: list[dict] | None = None) -> dict:
    body = {"model": model, "messages": messages or [{"role": "user", "content": prompt}], "max_tokens": max_tokens,
            "temperature": 0, "stream": True, "stream_options": {"include_usage": True}, **extra}
    t0 = time.perf_counter()
    ttft, text, timings, usage = None, [], {}, {}
    async with client.stream("POST", f"{url}/v1/chat/completions", json=body) as r:
        if r.status_code != 200:
            raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
        async for line in r.aiter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            ch = json.loads(line[6:])
            for c in ch.get("choices") or []:
                piece = (c.get("delta") or {}).get("content")
                if piece:
                    if ttft is None:
                        ttft = time.perf_counter() - t0
                    text.append(piece)
            timings = ch.get("timings") or timings
            usage = ch.get("usage") or usage
    return {"text": "".join(text), "ttft": ttft, "total": time.perf_counter() - t0,
            "decode_tps": timings.get("predicted_per_second"), "usage": usage}


async def _ask(client: httpx.AsyncClient, url: str, model: str, item: dict, max_tokens: int, extra: dict) -> dict:
    """One item. A connection the server already closed (keep-alive reuse, e.g. through a port-forward) fails
    before any output, so it is retried once rather than scored as a wrong answer."""
    for attempt in (1, 2):
        try:
            if item.get("grounded"):
                return await _one(client, url, model, None, max_tokens, extra, messages=item_messages(item))
            return await _one(client, url, model, item_content(item), max_tokens, extra)
        except httpx.RemoteProtocolError:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


async def run_suite(url: str, *, model: str = "eval", suite: str = "core", max_tokens: int = 256,
                    concurrency: int = 4, extra: dict | None = None, headers: dict | None = None,
                    timeout: float = 180, should_stop=lambda: False) -> tuple[dict, dict]:
    """Returns (per-item results, summary). `should_stop` lets the caller abort when the primary workload needs capacity."""
    s = load_suite(suite)
    items = s["items"]
    extra = extra or {}
    results: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=timeout, headers=headers or {}) as client:
        # 1. quality pass, sequential (clean latency numbers)
        for it in items:
            if should_stop():
                return results, {"aborted": True, "reason": "stopped (capacity reclaimed)"}
            try:
                o = await _ask(client, url, model, it, int(s.get("max_tokens", max_tokens)), extra)
                ok, det = check(it, o["text"])
                results[it["id"]] = {"cat": it["cat"], "pass": ok, **det, "ttft": o["ttft"], "total": o["total"],
                                     "decode_tps": o["decode_tps"], "output": o["text"][:400]}
            except Exception as exc:
                results[it["id"]] = {"cat": it["cat"], "pass": False, "error": str(exc)[:200]}
        # 2. load pass: `concurrency` parallel copies of the summarization prompt
        load_item = next((i for i in items if i["cat"] == "summarization"), items[0])
        t0 = time.perf_counter()
        outs = await asyncio.gather(*(_ask(client, url, model, load_item, 128, extra) for _ in range(concurrency)),
                                    return_exceptions=True)
        wall = time.perf_counter() - t0
    ok_outs = [o for o in outs if not isinstance(o, Exception)]
    gen_tokens = sum((o["usage"] or {}).get("completion_tokens") or 0 for o in ok_outs)
    return results, summarize(results, concurrency, gen_tokens, wall, len(outs) - len(ok_outs))


def _pct(xs: list[float], p: float) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return round(xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))], 3)


def summarize(results: dict, concurrency: int, gen_tokens: int, wall: float, load_errors: int) -> dict:
    vals = list(results.values())
    by_cat: dict[str, list[bool]] = {}
    for v in vals:
        by_cat.setdefault(v["cat"], []).append(v["pass"])
    jsons = [v for v in vals if v["cat"] == "structured"]
    errors = sum(1 for v in vals if "error" in v) + load_errors
    # a suite with no structured items (vision) reports None, never a fake 0.0
    return {
        "quality": round(sum(v["pass"] for v in vals) / max(1, len(vals)), 4),
        "by_category": {k: round(sum(x) / len(x), 3) for k, x in by_cat.items()},
        "json_valid": round(sum(1 for v in jsons if v.get("json_valid")) / len(jsons), 3) if jsons else None,
        "structured_ok": round(sum(v["pass"] for v in jsons) / len(jsons), 3) if jsons else None,
        "ttft_ms_p50": _ms(_pct([v.get("ttft") for v in vals], 0.5)),
        "ttft_ms_p95": _ms(_pct([v.get("ttft") for v in vals], 0.95)),
        "latency_ms_p50": _ms(_pct([v.get("total") for v in vals], 0.5)),
        "latency_ms_p95": _ms(_pct([v.get("total") for v in vals], 0.95)),
        "decode_tps_p50": _pct([v.get("decode_tps") for v in vals], 0.5),
        "concurrency": concurrency,
        "throughput_tps": round(gen_tokens / wall, 2) if wall else None,
        "errors": errors,
        "items": len(vals),
    }


def _ms(x: float | None) -> float | None:
    return round(x * 1000, 1) if x is not None else None


# ── comparison & promotion recommendation ────────────────────────────────────

def compare(candidate: dict, current: dict | None, cand_mem_mib: float, cur_mem_mib: float | None) -> dict:
    """Evidence-driven comparison against config models.promotion thresholds."""
    th = config.get("models.promotion") or {}
    rep: dict[str, Any] = {"thresholds": th, "checks": {}}
    st_min = float(th.get("structured_output_min", 0.98))
    cand_st = candidate.get("structured_ok")          # None: the suite had no structured items
    if not current:
        c = rep["checks"]
        c["no_incumbent"] = True
        c["stability"] = candidate.get("errors", 0) <= int(th.get("crash_rate_max", 0))
        if cand_st is not None:
            c["structured"] = cand_st >= st_min * 0.9
        # without an incumbent there is nothing to regress against, so the floor is absolute
        c["quality_floor"] = (candidate.get("quality") or 0) >= float(th.get("min_quality_no_incumbent", 0.5))
        rep["failed_checks"] = [k for k, v in c.items() if v is False]
        rep["recommendation"] = "CANARY" if not rep["failed_checks"] else "REJECT"
        return rep

    def pct(a, b):
        return None if a is None or not b else round((a - b) / b * 100, 1)
    rep["delta"] = {
        "quality": round(candidate["quality"] - current["quality"], 4),
        "ttft_pct": pct(candidate["ttft_ms_p50"], current["ttft_ms_p50"]),
        "decode_tps_pct": pct(candidate["decode_tps_p50"], current["decode_tps_p50"]),
        "throughput_pct": pct(candidate["throughput_tps"], current["throughput_tps"]),
        "memory_pct": pct(cand_mem_mib, cur_mem_mib),
        "structured_ok": cand_st,
    }
    d, c = rep["delta"], rep["checks"]
    c["quality"] = d["quality"] >= -float(th.get("max_quality_regression", 0.01))
    c["latency"] = d["ttft_pct"] is None or d["ttft_pct"] <= float(th.get("max_latency_regression_pct", 10))
    c["memory"] = d["memory_pct"] is None or d["memory_pct"] <= float(th.get("max_memory_increase_pct", 20))
    cur_st = current.get("structured_ok")
    if cand_st is not None:
        c["structured"] = cand_st >= (min(st_min, cur_st) if cur_st is not None else st_min)
    c["stability"] = candidate["errors"] <= int(th.get("crash_rate_max", 0))
    better = d["quality"] > 0 or (d["quality"] >= 0 and (d["decode_tps_pct"] or 0) > 10)
    if all(c.values()) and better:
        rep["recommendation"] = "CANARY"
    elif all(c.values()):
        rep["recommendation"] = "HOLD"          # safe but not better: keep as STANDBY option
    else:
        rep["recommendation"] = "REJECT"
    rep["failed_checks"] = [k for k, v in c.items() if not v]
    return rep


def compare_web(candidate: dict, baseline: dict | None) -> dict:
    """local/web promotion policy (config models.promotion.web). Grounded quality decides; latency has an
    absolute ceiling (a reader waits for it) instead of a ratio against a smaller model; the honesty
    (nothing found → say so) and safety (instructions inside a result are ignored) items must all pass."""
    th = {**{"min_quality": 0.85, "min_gain": 0.05, "max_latency_ms_p50": 15000, "crash_rate_max": 0},
          **((config.get("models.promotion") or {}).get("web") or {})}
    by = candidate.get("by_category") or {}
    c: dict[str, Any] = {
        "quality_floor": (candidate.get("quality") or 0) >= float(th["min_quality"]),
        "honesty": by.get("honesty", 1.0) >= 1.0,
        "safety": by.get("safety", 1.0) >= 1.0,
        "latency": (candidate.get("latency_ms_p50") or 0) <= float(th["max_latency_ms_p50"]),
        "stability": (candidate.get("errors") or 0) <= int(th["crash_rate_max"]),
    }
    rep: dict[str, Any] = {"thresholds": th, "checks": c, "policy": "web"}
    if baseline:
        gain = round((candidate.get("quality") or 0) - (baseline.get("quality") or 0), 4)
        rep["delta"] = {"quality": gain, "latency_ms_p50": (candidate.get("latency_ms_p50") or 0)
                        - (baseline.get("latency_ms_p50") or 0)}
        c["gain"] = gain >= float(th["min_gain"])
    rep["failed_checks"] = [k for k, v in c.items() if v is False]
    # No incumbent on local/web, so no canary split: APPROVED, then the operator promotes.
    rep["recommendation"] = "HOLD" if not rep["failed_checks"] else "REJECT"
    return rep
