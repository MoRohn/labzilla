"""local-ai — operator CLI for the Local Intelligence Fabric.

Talks to the controller (admin key), the gateway (API key) and the decision-fabric
service. Every mutating action goes through the controller so it is audited in the
activity timeline.

Environment:
  LIF_CONTROLLER_URL  default http://127.0.0.1:18081
  LIF_GATEWAY_URL     default http://127.0.0.1:18080
  LIF_DECISION_URL    default http://127.0.0.1:18082
  LIF_ADMIN_KEY       controller admin key
  LIF_API_KEY         gateway API key
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx

from lif.cli import decision_cmds, explain_cmds


class CliError(Exception):
    pass


class Api:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.controller = os.environ.get("LIF_CONTROLLER_URL", "http://127.0.0.1:18081").rstrip("/")
        self.gateway = os.environ.get("LIF_GATEWAY_URL", "http://127.0.0.1:18080").rstrip("/")
        self.decision = os.environ.get("LIF_DECISION_URL", "http://127.0.0.1:18082").rstrip("/")
        self.admin_key = os.environ.get("LIF_ADMIN_KEY", "")
        self.api_key = os.environ.get("LIF_API_KEY", "")
        self.http = httpx.Client(timeout=30, transport=transport)

    def _call(self, method: str, url: str, key: str, body: Any = None) -> Any:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            r = self.http.request(method, url, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise CliError(f"cannot reach {url}: {e}") from e
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {"raw": r.text[:500]}
        if r.status_code >= 400:
            msg = data.get("error") if isinstance(data, dict) else None
            if isinstance(msg, dict):
                msg = msg.get("message")
            raise CliError(f"HTTP {r.status_code}: {msg or data}")
        return data

    def ctl(self, method: str, path: str, body: Any = None) -> Any:
        return self._call(method, self.controller + path, self.admin_key, body)

    def gw(self, method: str, path: str, body: Any = None) -> Any:
        return self._call(method, self.gateway + path, self.api_key, body)

    def dec(self, method: str, path: str, body: Any = None) -> Any:
        return self._call(method, self.decision + path, "", body)


# ── formatting ───────────────────────────────────────────────────────────────

def table(rows: list[list[Any]], headers: list[str]) -> str:
    cells = [[str(h) for h in headers]] + [["" if c is None else str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in cells]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def fmt_pct(x: Any) -> str:
    return "-" if x is None else f"{float(x) * 100:.1f}%"


def fmt_ts(ts: Any) -> str:
    return "-" if not ts else time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))


def out(args, data: Any, render) -> None:
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(render(data))


# ── commands ─────────────────────────────────────────────────────────────────

def render_status(o: dict) -> str:
    b = o.get("blerbz") or {}
    caps = (o.get("capabilities") or {}).get("aliases") or {}
    lines = ["LOCAL INTELLIGENCE FABRIC", "",
             f"Workload state      {b.get('state', '?')}  ({b.get('reason', '')})",
             f"GPU admissible      {b.get('admissible_mib', 0):.0f} MiB   MemAvailable {b.get('mem_available_mib', 0):.0f} MiB",
             ""]
    rows = [[a, "READY" if v.get("available") else "UNAVAILABLE", v.get("served_by", ""),
             "yes" if v.get("fallback") else "", "yes" if v.get("degraded") else "", (v.get("reason") or "")[:60]]
            for a, v in sorted(caps.items())]
    lines.append(table(rows, ["alias", "status", "served_by", "fallback", "degraded", "reason"]) if rows
                 else "gateway capabilities unavailable: " + str((o.get("capabilities") or {}).get("error")))
    df = o.get("decision_fabric") or {}
    lines += ["", f"Decision Fabric     jev_enabled={df.get('jev_enabled')}  breaker_open={df.get('jev_breaker_open')}"
                  f"  model={df.get('jev_model')}"]
    av = o.get("availability_24h") or {}
    if av:
        lines += ["", "Measured availability (24 h):"]
        lines.append(table([[k, fmt_pct(v.get("availability")), v.get("samples")] for k, v in sorted(av.items())],
                           ["capability", "availability", "samples"]))
    models = o.get("models") or {}
    lines += ["", "Models: " + "  ".join(f"{k}={v}" for k, v in models.items() if v)]
    return "\n".join(lines)


def cmd_status(api: Api, args) -> int:
    out(args, api.ctl("GET", "/v1/overview"), render_status)
    return 0


def cmd_doctor(api: Api, args) -> int:
    checks: list[tuple[str, str, str]] = []
    try:
        o = api.ctl("GET", "/v1/overview")
        checks.append(("PASS", "controller", "reachable"))
    except CliError as e:
        checks.append(("FAIL", "controller", str(e)))
        o = {}
    try:
        h = api.gw("GET", "/v1/health")
        checks.append(("PASS" if h.get("useful_local_ai") else "FAIL", "gateway",
                       f"status={h.get('status')} useful_local_ai={h.get('useful_local_ai')}"))
    except CliError as e:
        checks.append(("FAIL", "gateway", str(e)))
    caps = (o.get("capabilities") or {}).get("aliases") or {}
    for a, v in sorted(caps.items()):
        if v.get("available"):
            lvl = "WARN" if v.get("fallback") or v.get("degraded") else "PASS"
            checks.append((lvl, f"alias {a}", f"{v.get('served_by')} {v.get('reason') or ''}".strip()))
        else:
            # vision/rerank are optional (installed only after discovery + benchmark + promotion) → WARN, not FAIL
            lvl = "FAIL" if a in ("local/fast", "local/default", "local/embedding") else "WARN"
            checks.append((lvl, f"alias {a}", v.get("reason", "unavailable")))
    b = o.get("blerbz") or {}
    if b:
        checks.append(("PASS" if b.get("reachable") else "WARN", "gpusched",
                       f"{b.get('state')} — {b.get('reason')}"))
    df = o.get("decision_fabric") or {}
    if "error" in df or not df:
        checks.append(("WARN", "decision fabric", str(df.get("error", "no status"))))
    else:
        lvl = "PASS" if df.get("jev_enabled") and not df.get("jev_breaker_open") else "WARN"
        checks.append((lvl, "decision fabric", f"jev_enabled={df.get('jev_enabled')} breaker_open="
                                               f"{df.get('jev_breaker_open')} (rules fallback always available)"))
    bt = o.get("batch") or {}
    checks.append(("WARN" if "error" in bt or not bt else "PASS", "batch engine",
                   str(bt.get("error", "reachable")) if ("error" in bt or not bt) else
                   f"paused={bt.get('paused')}"))
    if args.json:
        print(json.dumps([{"level": l, "check": c, "detail": d} for l, c, d in checks], indent=2))
    else:
        for l, c, d in checks:
            print(f"[{l}] {c:<24} {d}")
    return 1 if any(l == "FAIL" for l, _, _ in checks) else 0


def cmd_gpu(api: Api, args) -> int:
    def r(g):
        res = g.get("residents_loaded") or {}
        return "\n".join([
            f"workload state    {g.get('state')}  ({g.get('reason')})",
            f"production live   {g.get('production_live')}  leases={g.get('production_leases')}",
            f"forecast P(1h)    {fmt_pct(g.get('p_next_hour'))}  authoritative={g.get('forecast_authoritative')}",
            f"admissible        {g.get('admissible_mib', 0):.0f} MiB",
            f"MemAvailable      {g.get('mem_available_mib', 0):.0f} MiB",
            f"GPU util          {g.get('gpu_util_percent')}%",
            "residents         " + ", ".join(f"{k}={'loaded' if v else 'NOT LOADED'}" for k, v in res.items()),
            f"data age          {g.get('age_sec')} s"])
    out(args, api.ctl("GET", "/v1/gpu"), r)
    return 0


def render_models(d: dict) -> str:
    rows = []
    for m in d.get("models") or []:
        s = ((m.get("last_benchmark") or {}).get("summary") or {})
        p = m.get("profile") or {}
        rows.append([m["id"], m["state"], m.get("category"), p.get("device", ""), p.get("params_b", ""),
                     s.get("quality", ""), s.get("decode_tps_p50", ""), ",".join(m.get("aliases") or []),
                     ("pin " if m.get("pinned") else "") + ("BLOCKED" if m.get("blocked") else "")])
    return table(rows, ["model", "state", "category", "device", "params_b", "quality", "tok/s", "aliases", "flags"]) \
        if rows else "no models"


def render_candidates(d: dict) -> str:
    rows = []
    for m in d.get("models") or []:
        sc = m.get("screening") or {}
        rows.append([m["id"], m["category"], sc.get("priority", ""), (m.get("fit") or {}).get("verdict", ""),
                     (m.get("fit") or {}).get("est_cpu_decode_tps", ""),
                     ((sc.get("recommendation") or {}).get("why") or "")[:70]])
    return table(rows, ["candidate", "category", "priority", "fit", "est tok/s", "evidence"]) if rows \
        else "no candidates — run `local-ai models refresh`"


def render_funnel(run: dict) -> str:
    lines = [f"discovery run {run.get('id')}: {run.get('status')}  {run.get('error') or ''}".rstrip()]
    for cat, f in ((run.get("funnel") or {}).get("categories") or {}).items():
        lines.append(f"  {cat:<10} listed {f.get('listed')} → listing filter {f.get('after_listing_filter')} → "
                     f"details {f.get('detail_fetched')} → deterministic {f.get('after_deterministic')} → "
                     f"screened {f.get('after_screening')} → shortlisted {len(f.get('shortlisted') or [])}"
                     f"   (jev calls {f.get('jev_calls')}, {f.get('screen_ms')} ms, providers {f.get('providers')})")
        for name in f.get("shortlisted") or []:
            lines.append(f"      + {name}")
    return "\n".join(lines)


def cmd_models(api: Api, args) -> int:
    a = args.action
    if a == "list":
        out(args, api.ctl("GET", "/v1/models"), render_models)
    elif a == "candidates":
        out(args, api.ctl("GET", "/v1/models?state=CANDIDATE,STAGED,APPROVED,CANARY"), render_candidates)
    elif a == "refresh":
        body = {"categories": args.categories} if args.categories else {}
        res = api.ctl("POST", "/v1/models/refresh", body)
        if not args.wait:
            out(args, res, lambda d: f"discovery {d.get('status')} (watch: local-ai models refresh --wait)")
            return 0
        time.sleep(1)
        while True:
            runs = api.ctl("GET", "/v1/discovery/runs")
            if not runs.get("running") and runs.get("runs"):
                out(args, runs["runs"][0], render_funnel)
                return 0 if runs["runs"][0].get("status") == "succeeded" else 1
            time.sleep(3)
    elif a == "nominate":
        if not args.target or not args.file or not args.category:
            raise CliError("models nominate needs REPO, --file and --category")
        body = {"hf_repo": args.target, "file": args.file, "category": args.category}
        for k in ("revision", "active_params_b", "context", "concurrency", "id"):
            if getattr(args, k, None) is not None:
                body[k] = getattr(args, k)
        if args.arch:
            try:
                body["arch"] = json.loads(args.arch)
            except json.JSONDecodeError as exc:
                raise CliError(f"--arch is not JSON: {exc}") from exc
        if args.template_kwargs:
            try:
                body["chat_template_kwargs"] = json.loads(args.template_kwargs)
            except json.JSONDecodeError as exc:
                raise CliError(f"--template-kwargs is not JSON: {exc}") from exc
        out(args, api.ctl("POST", "/v1/models/nominate", body),
            lambda d: f"{d.get('id')} → {d.get('state')} for {d.get('alias')} "
                      f"(fit {(d.get('fit') or {}).get('verdict')}, ~{(d.get('fit') or {}).get('est_cpu_decode_tps')} tok/s, "
                      f"{(d.get('profile') or {}).get('memory_budget_mb')} MiB limit). Next: local-ai models download {d.get('id')}")
    elif a == "rollback":
        body = {"alias": args.target}
        if args.to_version is not None:
            body["to_version"] = args.to_version
        out(args, api.ctl("POST", "/v1/aliases/rollback", body),
            lambda d: f"{d.get('alias')} → v{d.get('version')}: {d.get('chain')}")
    else:
        if not args.target:
            raise CliError(f"models {a} needs a MODEL argument")
        body: dict[str, Any] = {}
        if getattr(args, "force", False):
            body["force"] = True
        if getattr(args, "alias", None):
            body["alias"] = args.alias
        if getattr(args, "percent", None) is not None:
            body["percent"] = args.percent
        if getattr(args, "suite", None):
            body["suite"] = args.suite
        out(args, api.ctl("POST", f"/v1/models/{args.target}/{a}", body),
            lambda d: json.dumps(d, indent=2, default=str))
    return 0


def cmd_decision(api: Api, args) -> int:
    if args.action not in ("status", "workflows", "metrics"):
        return decision_cmds.cmd(api, args)
    if args.action == "workflows":
        out(args, api.dec("GET", "/decision/workflows"), lambda d: json.dumps(d, indent=2, default=str))
        return 0
    st = api.dec("GET", "/decision/status")
    if args.action == "status":
        out(args, st, lambda d: "\n".join(f"{k:<20} {v}" for k, v in d.items() if not isinstance(v, (list, dict))))
    else:
        out(args, st, lambda d: "\n".join([
            f"decisions total     {d.get('decisions_total')}",
            f"by provider         {d.get('by_provider')}",
            f"by action           {d.get('by_action')}",
            f"latency p50         {d.get('latency_ms_p50')} ms",
            f"cache hit rate      {fmt_pct(d.get('cache_hit_rate'))}"]))
    return 0


def cmd_batch(api: Api, args) -> int:
    if args.action == "list":
        def r(d):
            rows = [[b.get("id"), b.get("status"), b.get("priority"), b.get("model"),
                     f"{b.get('completed', 0)}/{b.get('total', 0)}", b.get("failed", 0), fmt_ts(b.get("created"))]
                    for b in (d.get("batches") or d.get("data") or [])]
            return table(rows, ["id", "status", "priority", "model", "done", "failed", "created"]) if rows \
                else "no batches"
        out(args, api.gw("GET", "/v1/batch"), r)
    else:
        paused = args.action == "pause"
        out(args, api.ctl("POST", "/v1/settings", {"batch_paused": paused}),
            lambda d: f"batch_paused = {d.get('batch_paused')}")
    return 0


def cmd_maintenance(api: Api, args) -> int:
    on = args.state == "on"
    out(args, api.ctl("POST", "/v1/settings", {"maintenance": on}),
        lambda d: f"maintenance = {d.get('maintenance')} (batch and candidate work {'paused' if on else 'allowed'})")
    return 0


def cmd_activity(api: Api, args) -> int:
    def r(d):
        rows = [[fmt_ts(e["ts"]), e["kind"], e.get("subject", ""), e.get("actor", ""),
                 json.dumps(e.get("detail") or {}, default=str)[:90]] for e in reversed(d.get("activity") or [])]
        return table(rows, ["time", "event", "subject", "actor", "detail"]) if rows else "no activity"
    out(args, api.ctl("GET", f"/v1/activity?limit={args.limit}"), r)
    return 0


MODEL_ACTIONS = ["list", "refresh", "candidates", "benchmark", "load", "unload", "promote", "canary", "rollback",
                 "pin", "unpin", "block", "unblock", "download", "nominate"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="local-ai", description="Local Intelligence Fabric operator CLI")
    p.add_argument("--json", action="store_true", help="raw JSON output")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("gpu")
    m = sub.add_parser("models")
    m.add_argument("action", choices=MODEL_ACTIONS)
    m.add_argument("target", nargs="?", help="MODEL id (or ALIAS for rollback)")
    m.add_argument("--categories", nargs="+")
    m.add_argument("--wait", action="store_true")
    m.add_argument("--force", action="store_true")
    m.add_argument("--alias")
    m.add_argument("--suite", choices=["core", "vision", "web", "embedding"],
                   help="benchmark: run this suite (another suite only on a live model)")
    m.add_argument("--file", help="nominate: the GGUF file in the repo")
    m.add_argument("--revision", help="nominate: 40-char commit (default: the repo's current one)")
    m.add_argument("--category", help="nominate: fast|general|coding|reasoning|embedding|reranking|vision|web")
    m.add_argument("--active-params-b", type=float, help="nominate: active parameters of an MoE model (billions)")
    m.add_argument("--template-kwargs", help='nominate: chat_template_kwargs JSON, e.g. \'{"reasoning_effort": "low"}\'')
    m.add_argument("--arch", help='nominate: shape from the model card when the repo lacks it, JSON '
                                   '{"num_layers", "num_kv_heads", "head_dim", "vocab_size"}')
    m.add_argument("--context", type=int)
    m.add_argument("--concurrency", type=int)
    m.add_argument("--id", help="nominate: profile id (default: derived from repo and quant)")
    m.add_argument("--percent", type=float)
    m.add_argument("--to-version", type=int, dest="to_version")
    d = sub.add_parser("decision")
    d.add_argument("action", choices=decision_cmds.DE_ACTIONS)
    decision_cmds.add_parsers(sub, d)
    b = sub.add_parser("batch")
    b.add_argument("action", choices=["list", "pause", "resume"])
    mt = sub.add_parser("maintenance")
    mt.add_argument("state", choices=["on", "off"])
    a = sub.add_parser("activity")
    a.add_argument("--limit", type=int, default=50)
    explain_cmds.add_parser(sub)
    for sp in sub.choices.values():
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    return p


HANDLERS = {"status": cmd_status, "doctor": cmd_doctor, "gpu": cmd_gpu, "models": cmd_models,
            "decision": cmd_decision, "batch": cmd_batch, "maintenance": cmd_maintenance, "activity": cmd_activity,
            "agent": decision_cmds.cmd_agent, "workflow": decision_cmds.cmd_workflow,
            "explain": explain_cmds.cmd_explain}


def main(argv: list[str] | None = None, api: Api | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not hasattr(args, "json"):
        args.json = False
    try:
        return HANDLERS[args.cmd](api or Api(), args)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
