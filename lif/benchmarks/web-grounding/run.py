"""Grounded-QA benchmark: does a local model answer recent-information questions correctly from live evidence?

Each question is classified (lif.web.freshness), looked up ONCE (lif.web.ground: feeds + SearXNG), and the
same grounded messages go to every model arm, so arms differ only by model. Scored per answer:

  fact    every `expect` regex matches          cite   an inline [n] citation is present (grounded rows)
  clean   no cutoff disclaimer and no hedging   ms     model wall time (+ llama.cpp prompt/decode timings)

Run (host, concurrency 1; waits while the primary workload is HIGH/IMMINENT or MemAvailable < 8 GiB):
  kubectl -n ai-system port-forward svc/searxng 18888:8080 &
  kubectl -n ai-serving port-forward svc/tier0 18080:8080 &          # one port-forward per arm
  LIF_CONTROLLER_URL=http://<controller clusterIP>:8080 LIF_ADMIN_KEY=$(cat ../secrets/lif-admin.key) \\
  SEARXNG_URL=http://localhost:18888 ARMS="qwen3-4b=http://localhost:18080" \\
  .venv/bin/python benchmarks/web-grounding/run.py [questions.json]
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lif.web import ground as wg                         # noqa: E402
from lif.web.freshness import assess_turn                # noqa: E402
from lif.web.search import SearxngProvider               # noqa: E402
from lif.web.sports import SportsFeed                    # noqa: E402
from lif.web.weather import WeatherFeed                  # noqa: E402

HERE = Path(__file__).parent
CTL, ADMIN = os.environ.get("LIF_CONTROLLER_URL"), os.environ.get("LIF_ADMIN_KEY")
MIN_HEADROOM_MIB = 8192
HEDGE = re.compile(r"(?i)hypothetical|fictional|knowledge cut-?off|as of my (?:last|latest)|real-?time (?:data|access|"
                   r"information)|cannot (?:verify|confirm) (?:the )?(?:date|this)|future (?:date|timeline|event)|"
                   r"not (?:yet )?(?:occurred|happened)")


async def wait_for_capacity(http: httpx.AsyncClient) -> None:
    """Yield to the primary workload: its state AND host memory headroom (see tier0 memory gate)."""
    while True:
        st, mem = "UNKNOWN", 0.0
        if CTL and ADMIN:
            try:
                d = (await http.get(f"{CTL}/v1/gpu", headers={"Authorization": f"Bearer {ADMIN}"}, timeout=5)).json()
                st, mem = d.get("state", "UNKNOWN"), float(d.get("mem_available_mib") or 0)
            except Exception:
                pass
        if st in ("LOW", "MODERATE") and mem >= MIN_HEADROOM_MIB:
            return
        print(f"  waiting: primary workload {st}, MemAvailable {mem:.0f} MiB", flush=True)
        await asyncio.sleep(60)


def arms() -> dict[str, str]:
    out = {}
    for part in (os.environ.get("ARMS") or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip().rstrip("/")
    if not out:
        sys.exit("set ARMS=name=http://host:port[,name2=...]")
    return out


def arm_kwargs() -> dict[str, dict]:
    return json.loads(os.environ.get("ARM_KWARGS") or "{}")


def score(text: str, expect: list[str], grounded: bool) -> dict:
    return {"fact": all(re.search(p, text) for p in expect), "cite": bool(re.search(r"\[\d+\]", text)) or not grounded,
            "clean": not wg.is_disclaimer(text) and not HEDGE.search(text)}


async def main() -> None:
    qfile = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "questions-20261003.json"
    spec = json.loads(qfile.read_text())
    tz = spec.get("timezone", "UTC")
    grounder = wg.Grounder(SearxngProvider(os.environ.get("SEARXNG_URL", "http://localhost:18888"), timeout=3),
                           SportsFeed(), weather=WeatherFeed())
    models = arms()
    rows = []
    async with httpx.AsyncClient(timeout=240) as http:
        for item in spec["questions"]:
            q = item["q"]
            await wait_for_capacity(http)
            now = wg.now_in(tz)
            f = assess_turn(q, [])
            classified_ok = f.live == item.get("live", True)
            g = None
            msgs = wg.inject_date([{"role": "user", "content": q}], now)
            if f.live:
                g = await grounder.ground(q, [], now, f.kind if f.kind not in ("none", "clock") else "general")
                msgs = wg.inject_evidence(msgs, g, now) if g.status == "ok" else wg.inject_unavailable(msgs, g, now)
            row = {"q": q, "live": f.live, "classified_ok": classified_ok, "kind": f.kind,
                   "lookup": g.meta() if g else None, "arms": {}}
            for name, url in models.items():
                await wait_for_capacity(http)
                t = time.perf_counter()
                # As the gateway sends it (profile chat_template_kwargs): Qwen3 hybrids without a thinking pass;
                # other templates via ARM_KWARGS='{"gpt-oss-20b": {"reasoning_effort": "low"}}'.
                kw = arm_kwargs().get(name, {"enable_thinking": False})
                body = {"messages": msgs, "max_tokens": 512, "temperature": 0.2, "chat_template_kwargs": kw}
                try:
                    r = await http.post(f"{url}/v1/chat/completions", json=body)
                except httpx.RemoteProtocolError:      # a kept-alive connection the port-forward already closed
                    r = await http.post(f"{url}/v1/chat/completions", json=body)
                ms = (time.perf_counter() - t) * 1000
                d = r.json()
                text = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                text = re.sub(r"(?s)<think>.*?</think>", "", text).strip()
                tm = d.get("timings") or {}
                row["arms"][name] = {"answer": text, "ms": round(ms), **score(text, item.get("expect", []), bool(g)),
                                     "prompt_tokens": (d.get("usage") or {}).get("prompt_tokens"),
                                     "completion_tokens": (d.get("usage") or {}).get("completion_tokens"),
                                     "cached_tokens": ((d.get("usage") or {}).get("prompt_tokens_details") or {}).get("cached_tokens"),
                                     "prompt_ms": round(tm.get("prompt_ms", 0)), "decode_tps": round(tm.get("predicted_per_second", 0), 1)}
                a = row["arms"][name]
                print(f"[{name}] {'✓' if a['fact'] else '✗'}fact {'✓' if a['cite'] else '✗'}cite "
                      f"{'✓' if a['clean'] else '✗'}clean {a['ms']:>6} ms  {q}", flush=True)
            rows.append(row)
    summary = {}
    for name in models:
        xs = [r["arms"][name] for r in rows]
        live = [r["arms"][name] for r in rows if r["live"]]
        summary[name] = {"n": len(xs), "fact": sum(x["fact"] for x in xs), "cite_live": sum(x["cite"] for x in live),
                         "clean": sum(x["clean"] for x in xs), "n_live": len(live),
                         "median_ms": round(statistics.median(x["ms"] for x in xs)),
                         "p90_ms": round(sorted(x["ms"] for x in xs)[int(0.9 * (len(xs) - 1))])}
    lookups = [r["lookup"]["ms"] for r in rows if r["lookup"]]
    out = {"date": time.strftime("%Y-%m-%d %H:%M"), "questions": str(qfile.name), "arms": models,
           "classifier_correct": sum(r["classified_ok"] for r in rows), "n": len(rows),
           "lookup_ms_median": round(statistics.median(lookups)) if lookups else None,
           "lookup_ms_max": round(max(lookups)) if lookups else None, "summary": summary, "rows": rows}
    path = HERE / f"results-{time.strftime('%Y%m%d-%H%M')}.json"
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}, indent=1))
    print(f"wrote {path}")


if __name__ == "__main__":
    asyncio.run(main())
