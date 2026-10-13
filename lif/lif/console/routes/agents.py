"""Agents and approvals: /api/agents/* (runs synthesized from real sources) and /api/approvals/* (spec §22–§25, §40).

LIF has no persisted agent-run entity (understand.json: AgentLoop results are in memory and unused in
production). The Agents page therefore shows the work that *is* recorded, labelled by where it came from:
- Model Scout   ← controller /v1/discovery/runs (durable; stage counts exist only once a run finishes)
- Evaluator     ← controller activity (state_changed / load_test_passed / benchmark_completed /
                  comparison_complete for one model) and running `benchmark:<mid>` tasks
- Decision Tuner← /v1/de/cycle (the last improvement cycle, in memory on the decision service)
- mined traces  ← /v1/de/traces (agent runs reconstructed from traces: step counts, no goal or result)
Nothing is invented: no "Code Agent", no fake progress, no cost in dollars that nobody recorded.

Runnable today: Model Scout (POST /v1/models/refresh) and the Evaluator (POST /v1/models/{mid}/benchmark).

Approvals: Decision Engineering review tickets (/v1/de/human) are labelled honestly as reviews — answering
one teaches the decision system; nothing is waiting on it — and device pairings (CORE's pairing table)
are real gates. Ticket state (inputs) is never shown: only the question, the options and why it was asked.

Owner: AI.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Depends

from lif.common import log
from lif.console import auth, humanize, poller, upstream
from lif.console.contracts import (AgentCatalogItem, AgentParam, AgentParamOption, AgentRun, AgentRunRequest,
                                   AgentsOverview, Approval, ApprovalAnswer, ApprovalOption, ArtifactLink,
                                   DecisionBadge, Fact, OkResponse, TechDetail, TimelineStep, User)
from lif.console.errors import human
from lif.console.events import hub
from lif.console.upstream import UpstreamError

LOG = log.get("lif.console.agents")

router = APIRouter(prefix="/api", tags=["agents"])     # spans areas: paths include /agents or /approvals

RECENT_LIMIT = 20
EVAL_WINDOW_SEC = 2 * 3600          # activity for one benchmark run: load → bench → compare
SAFE_ID = re.compile(r"^[A-Za-z0-9._:@+-]{1,200}$")
TRACE_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")     # goes into an upstream path: no dots, no slashes


def _tech(**kv: Any) -> list[TechDetail]:
    return [TechDetail(label=k.replace("_", " "), value=str(v)) for k, v in kv.items() if v not in (None, "", [], {})]


def _model(mid: str | None) -> str:
    from lif.console.routes.ai import friendly_model   # one naming rule for models everywhere
    return friendly_model(mid) or (mid or "a model")


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


# ── Model Scout (discovery runs) ──────────────────────────────────────────────────────────────

def scout_run(run: dict[str, Any], running_id: Any) -> AgentRun:
    funnel = poller._d(run.get("funnel"))
    cats = {k: v for k, v in poller._d(funnel.get("categories")).items() if isinstance(v, dict)}
    started, finished = run.get("ts"), run.get("finished")
    st = run.get("status")
    interrupted = st == "running" and running_id != run.get("id")     # stale row after a controller restart
    steps = [TimelineStep(ts=started or 0.0, label="Started checking for better models",
                          detail=", ".join(cats) or None)]
    decisions: list[DecisionBadge] = []
    total = lambda k: sum(int(humanize.num(c.get(k)) or 0) for c in cats.values())   # noqa: E731
    listed, relevant, screened = total("listed"), total("after_deterministic"), total("after_screening")
    shortlisted = sum(len(c["shortlisted"]) for c in cats.values() if isinstance(c.get("shortlisted"), list))
    jev_calls = total("jev_calls")
    providers: dict[str, int] = {}
    for c in cats.values():
        for p, n in poller._d(c.get("providers")).items():
            providers[p] = providers.get(p, 0) + int(humanize.num(n) or 0)
    end = finished or started or 0.0
    if cats:
        steps += [
            TimelineStep(ts=end, label=f"Discovering: {_plural(listed, 'model')} found on Hugging Face"),
            TimelineStep(ts=end, label=f"Filtering: {relevant:,} relevant to this machine"),
            TimelineStep(ts=end, label=f"Evaluating: {screened:,} looked promising after screening", kind="decision"),
            TimelineStep(ts=end, label=f"{_plural(shortlisted, 'candidate')} shortlisted for benchmarking",
                         kind="success" if shortlisted else "info"),
        ]
        errs = {k: v.get("error") for k, v in cats.items() if v.get("error")}
        for k, e in errs.items():
            steps.append(TimelineStep(ts=end, label=f"Couldn't check {k} models", detail=str(e)[:200], kind="warning"))
        decisions.append(DecisionBadge(
            decision=f"Shortlist {shortlisted} of {screened} screened models", decided_by="jev" if providers.get("jev")
            else "rules", actionable=True,
            why=[Fact(label="Screened by Jev", value=str(providers.get("jev", 0))),
                 Fact(label="Screened by rules", value=str(providers.get("rules", 0))),
                 Fact(label="Next step", value="A person decides whether to download and benchmark")]))
    if interrupted:
        status, result = "failed", "Interrupted: the controller restarted during this run."
        steps.append(TimelineStep(ts=end, label="Interrupted by a controller restart", kind="error"))
    elif st == "running":
        status, result = "running", None
    elif st == "succeeded":
        status = "done"
        result = (f"{_plural(shortlisted, 'candidate')} shortlisted" if shortlisted else "No better models found")
        steps.append(TimelineStep(ts=end, label="Finished", kind="success"))
    else:
        status, result = "failed", f"Failed: {humanize.run_error(run.get('error'), 'no reason recorded')}"
        steps.append(TimelineStep(ts=end, label="Failed", detail=humanize.run_error(run.get("error"), "") or None,
                                  kind="error"))
    tools = ["Hugging Face search", "Hardware fit check"] + (["Jev screening"] if jev_calls else ["Rule screening"])
    return AgentRun(
        id=f"scout:{run.get('id')}", agent="Model Scout",
        task="Check for better models" + (f" ({', '.join(cats)})" if cats and len(cats) < 6 else ""),
        status=status, started_at=started, finished_at=finished, steps=steps, decisions=decisions,  # type: ignore[arg-type]
        current_step="Searching Hugging Face (stage counts appear when the run finishes)" if status == "running"
        else None, tools=tools, result=result, source="discovery",
        artifacts=[ArtifactLink(label="Candidates found", href="/models/discovery")] if shortlisted else [],
        cost_label=f"{_plural(jev_calls, 'Jev decision')} (cost not recorded per run)" if jev_calls else None,
        tech=_tech(run_id=run.get("id"), raw_status=st, total_ms=funnel.get("total_ms")))


# ── Evaluator (benchmark activity) ────────────────────────────────────────────────────────────

def _eval_step(row: dict[str, Any]) -> TimelineStep | None:
    kind, d, ts = row.get("kind"), row.get("detail") or {}, float(row.get("ts") or 0)
    if kind == "state_changed":
        to = str(d.get("to") or "").upper()
        label = {"VALIDATING": "Starting a test server", "BENCHMARKING": "Benchmarking",
                 "APPROVED": "Approved for a trial", "REJECTED": "Rejected",
                 "STAGED": "Stopped and put back in the queue"}.get(to)
        if label is None:
            return None
        k = "success" if to == "APPROVED" else "error" if to == "REJECTED" else "warning" if to == "STAGED" else "info"
        return TimelineStep(ts=ts, label=label, detail=str(d.get("reason") or "")[:200] or None, kind=k)  # type: ignore[arg-type]
    if kind == "load_test_passed":
        secs = d.get("load_sec")
        return TimelineStep(ts=ts, label="Test server ready" + (f" (loaded in {float(secs):.0f} s)" if secs else ""))
    if kind == "benchmark_completed":
        parts = []
        if isinstance(d.get("quality"), (int, float)):
            parts.append(f"quality {d['quality'] * 100:.0f}%")
        if isinstance(d.get("decode_tps_p50"), (int, float)):
            parts.append(f"{d['decode_tps_p50']:.1f} tokens/s")
        if d.get("errors"):
            parts.append(_plural(int(d["errors"]), "error"))
        return TimelineStep(ts=ts, label="Benchmark complete", detail=", ".join(parts) or None, kind="success")
    if kind == "comparison_complete":
        rec = str(d.get("recommendation") or "").upper()
        label = {"CANARY": "Recommended a trial on part of the traffic", "HOLD": "Recommended keeping it in reserve",
                 "REJECT": "Recommended rejecting it"}.get(rec, "Compared with the model in use")
        return TimelineStep(ts=ts, label=label, detail=f"Against {_model(d.get('incumbent'))}" if d.get("incumbent")
                            else None, kind="decision")
    return None


def evaluator_runs(activity: list[dict[str, Any]], tasks: dict[str, Any]) -> list[AgentRun]:
    """One run per benchmark_completed event, with that model's surrounding events as its timeline."""
    rows = sorted((r for r in activity if isinstance(r, dict)), key=lambda r: float(r.get("ts") or 0))
    out: list[AgentRun] = []
    for b in (r for r in rows if r.get("kind") == "benchmark_completed"):
        mid, ts = str(b.get("subject") or ""), float(b.get("ts") or 0)
        mine = [r for r in rows if str(r.get("subject") or "") == mid and ts - EVAL_WINDOW_SEC <= float(r.get("ts") or 0)
                <= ts + 600]
        steps = [s for s in (_eval_step(r) for r in mine) if s is not None]
        comp = next((r for r in mine if r.get("kind") == "comparison_complete" and float(r.get("ts") or 0) >= ts), None)
        decisions, result = [], "Benchmark complete"
        if comp:
            d = comp.get("detail") or {}
            rec = str(d.get("recommendation") or "").upper()
            jev = d.get("jev") if isinstance(d.get("jev"), dict) else {}
            result = {"CANARY": "Better than the model in use: ready for a trial",
                      "HOLD": "Not clearly better: kept in reserve",
                      "REJECT": "Worse than the model in use: rejected"}.get(rec, "Compared with the model in use")
            decisions.append(DecisionBadge(
                decision={"CANARY": "Recommend a canary trial", "HOLD": "Hold: keep as a backup",
                          "REJECT": "Reject the candidate"}.get(rec, rec.capitalize() or "Compared"),
                decided_by="code", actionable=True,
                confidence=float(jev["confidence"]) if isinstance(jev.get("confidence"), (int, float)) else None,
                why=[Fact(label="Compared with", value=_model(d.get("incumbent")))] +
                    ([Fact(label="Jev's view", value="Likely an improvement" if humanize.jev_improves(jev.get("improves"))
                           else "Not clearly an improvement")]
                     if humanize.jev_improves(jev.get("improves")) is not None else [])))
        started = min((s.ts for s in steps), default=ts)
        out.append(AgentRun(
            id=f"eval:{b.get('seq')}", agent="Evaluator", task=f"Benchmark {_model(mid)}", status="done",
            started_at=started, finished_at=max((s.ts for s in steps), default=ts), steps=steps, decisions=decisions,
            tools=["Temporary test server", "Core evaluation suite", "Comparison against the model in use"],
            artifacts=[ArtifactLink(label="Model details", href=f"/models/deployments/{mid}")], result=result,
            source="benchmark", tech=_tech(model=mid, activity_seq=b.get("seq"))))
    for key, value in (tasks or {}).items():
        kind, _, mid = str(key).partition(":")
        if kind == "benchmark" and value == "running":
            out.append(AgentRun(id=f"eval:running:{mid}", agent="Evaluator", task=f"Benchmark {_model(mid)}",
                                status="running", current_step="Benchmarking (no step-level progress is reported)",
                                tools=["Temporary test server", "Core evaluation suite"], source="benchmark",
                                tech=_tech(task=key)))
        elif kind == "benchmark" and str(value).startswith("failed"):
            out.append(AgentRun(id=f"eval:failed:{mid}", agent="Evaluator", task=f"Benchmark {_model(mid)}",
                                status="failed", result=str(value).partition(":")[2].strip()[:200] or "Failed",
                                source="benchmark", tech=_tech(task=key, raw_status=value)))
    return out


# ── Decision Tuner (/v1/de/cycle) and mined traces (/v1/de/traces) ────────────────────────────

def tuner_run(cycle: dict[str, Any]) -> AgentRun | None:
    if not isinstance(cycle, dict) or not cycle.get("started"):
        return None          # {note: 'no improvement cycle has run yet'}
    mined = cycle.get("mined") or {}
    cal = [c for c in cycle.get("calibrated") or [] if isinstance(c, dict)]
    recs = cycle.get("recommendations") or []
    started = float(cycle["started"])
    end = started + float(cycle.get("elapsed_s") or 0)
    steps = [TimelineStep(ts=started, label="Started an improvement cycle")]
    if mined:
        steps.append(TimelineStep(ts=end, label=f"Mined {_plural(int(mined.get('runs') or 0), 'agent run')} "
                                               f"({_plural(int(mined.get('operations') or 0), 'step')})"))
    steps.append(TimelineStep(ts=end, label=f"Calibrated {_plural(len(cal), 'decision')}",
                              detail=f"{sum(bool(c.get('adequate')) for c in cal)} with enough data" if cal else None))
    steps.append(TimelineStep(ts=end, label=f"{_plural(len(recs), 'recommendation')} — recommend-only, a person decides",
                              kind="decision" if recs else "info"))
    return AgentRun(id=f"tuner:{int(started)}", agent="Decision Tuner",
                    task="Learn from recent decisions and suggest threshold changes", status="done",
                    started_at=started, finished_at=end, steps=steps, tools=["Trace miner", "Calibration"],
                    result=f"{_plural(len(recs), 'recommendation')}; nothing changes without a person",
                    artifacts=[ArtifactLink(label="Decisions", href="/agents?tab=decisions")], source="decision_cycle",
                    tech=_tech(calibrated=", ".join(str(c.get("ref")) for c in cal[:10]) or None))


CLASS_LABEL = {"CODE": "rule", "JEV_CANDIDATE": "quick decision", "GENERATIVE": "writing/creation",
               "HUMAN_OR_POLICY": "needs a person", "UNKNOWN": "unclassified"}


def _human_id(raw: str) -> str:
    """'coding-agent' → 'Coding agent', 'model_discovery' → 'Model discovery'; names with capitals are kept."""
    if not raw or any(c.isupper() for c in raw):
        return raw
    words = raw.replace("-", " ").replace("_", " ").split()
    return " ".join(words).capitalize()


def trace_run(r: dict[str, Any], ops: list[dict[str, Any]] | None = None) -> AgentRun:
    """A run mined from traces. Traces record steps and their classification, not goals or outcomes."""
    counts = [(int(r.get(k) or 0), w) for k, w in (("code", "rule"), ("jev", "quick decision"),
                                                    ("gen", "writing step"), ("human", "step needing a person"))]
    summary = ", ".join(_plural(n, w) for n, w in counts if n) or "no classified steps"
    steps = []
    for op in ops or []:
        cls = CLASS_LABEL.get(str(op.get("classification") or "UNKNOWN"), "unclassified")
        detail = ", ".join(x for x in (cls, f"by {op['executor']}" if op.get("executor") else "",
                                       f"→ {op['output_label']}" if op.get("output_label") else "") if x)
        steps.append(TimelineStep(ts=float(op.get("ts") or r.get("t0") or 0), label=str(op.get("name") or op.get("kind")
                                  or "step"), detail=detail or None,
                                  kind="decision" if op.get("kind") == "decision" else "info"))
    workflow = _human_id(str(r.get("workflow") or ""))
    return AgentRun(id=f"trace:{r.get('run_id')}", agent=_human_id(str(r.get("agent") or "")) or "Agent",
                    task=f"Recorded {workflow[:1].lower()}{workflow[1:]}" if workflow else "Recorded agent work",
                    status="done", started_at=r.get("t0"),
                    finished_at=max((s.ts for s in steps), default=None), steps=steps,
                    result=f"{_plural(int(r.get('ops') or 0), 'step')} recorded: {summary}. Traces don't record "
                           "the goal or the outcome.", source="trace",
                    tech=_tech(run_id=r.get("run_id"), agent=r.get("agent"), workflow=r.get("workflow")))


# ── aggregation ───────────────────────────────────────────────────────────────────────────────

async def _get(path: str, **params: Any) -> Any:
    return await upstream.get("controller", path, params=params or None)


async def collect() -> tuple[list[AgentRun], list[str]]:
    snap = poller.snapshot()
    tasks = snap.raw.get("tasks") if isinstance(snap.raw.get("tasks"), dict) else \
        (snap.raw.get("overview") or {}).get("tasks") or {}
    calls = {"runs": _get("/v1/discovery/runs"), "activity": _get("/v1/activity", limit=500),
             "cycle": _get("/v1/de/cycle"), "traces": _get("/v1/de/traces", limit=10)}
    got = dict(zip(calls, await asyncio.gather(*calls.values(), return_exceptions=True)))
    ok = {k: v for k, v in got.items() if not isinstance(v, BaseException) and isinstance(v, dict)}
    notes = []
    if "runs" not in ok and "activity" not in ok:
        notes.append("The controller didn't answer, so Model Scout and Evaluator work isn't shown.")
    if "cycle" not in ok and "traces" not in ok:
        notes.append("Decision Engineering didn't answer, so decision cycles and traces aren't shown.")
    runs: list[AgentRun] = []
    disc = ok.get("runs") or {}
    runs += [scout_run(r, disc.get("running")) for r in (disc.get("runs") or [])[:10] if isinstance(r, dict)]
    runs += evaluator_runs((ok.get("activity") or {}).get("activity") or [], tasks)
    t = tuner_run(ok.get("cycle") or {})
    if t:
        runs.append(t)
    runs += [trace_run(r) for r in (ok.get("traces") or {}).get("runs") or [] if isinstance(r, dict)]
    return runs, notes


def catalog(user: User) -> list[AgentCatalogItem]:
    s = poller.snapshot().raw.get("settings") or {}
    off = "Maintenance mode is on." if s.get("maintenance") else \
        "Model discovery is switched off in Settings." if s.get("discovery_disabled") else None

    def gate(perm: str, extra: str | None = None) -> tuple[bool, str | None]:
        if perm not in user.perms:
            return False, "Needs an admin session."
        return (extra is None), extra

    scout_ok, scout_why = gate("models.discover", off)
    eval_ok, eval_why = gate("models.operate", "Maintenance mode is on." if s.get("maintenance") else None)
    return [
        AgentCatalogItem(
            id="model-scout", name="Model Scout", runnable=scout_ok, why_not=scout_why, perm="models.discover",
            description="Searches Hugging Face for models that could beat the ones in use, filters what fits "
                        "this machine and shortlists candidates. Uses a little paid Jev screening; downloads nothing.",
            params=[AgentParam(key="categories", label="Which kinds of model", type="multiselect",
                               options=[AgentParamOption(value=c, label=humanize.role_label(
                                   humanize.CATEGORY_ALIAS.get(c, c)) if c in humanize.CATEGORY_ALIAS else c.title())
                                   for c in humanize.DISCOVERY_CATEGORIES])]),
        AgentCatalogItem(
            id="evaluator", name="Evaluator", runnable=eval_ok, why_not=eval_why, perm="models.operate",
            description="Benchmarks one candidate on a temporary test server and compares it with the model in "
                        "use. Waits if the GPU or memory is short.",
            params=[AgentParam(key="candidate", label="Candidate model", type="candidate", required=True)]),
    ]


@router.get("/agents", response_model=AgentsOverview)
async def overview(user: User = Depends(auth.require("read"))) -> AgentsOverview:
    runs, notes = await collect()
    active = [r for r in runs if r.status in ("running", "queued", "waiting_approval")]
    recent = sorted((r for r in runs if r not in active), key=lambda r: r.finished_at or r.started_at or 0,
                    reverse=True)[:RECENT_LIMIT]
    notes.append("Agent work is reconstructed from what Labzilla records; there is no general-purpose agent "
                 "runtime yet.")
    return AgentsOverview(running=active, recent=recent, catalog=catalog(user), note=" ".join(notes))


@router.get("/agents/{run_id:path}", response_model=AgentRun)
async def get_run(run_id: str, user: User = Depends(auth.require("read"))) -> AgentRun:
    if not SAFE_ID.match(run_id):
        raise _run_not_found()
    if run_id.startswith("trace:"):
        rid = run_id.partition(":")[2]
        if not TRACE_ID.match(rid):
            raise _run_not_found()
        try:
            ops = await _get(f"/v1/de/traces/{rid}")
            listing = await _get("/v1/de/traces", limit=50)
        except UpstreamError as e:
            raise upstream.to_human(e, doing="load this agent run", not_found="Agent run not found") from e
        runs = poller._d(listing).get("runs")
        row = next((r for r in runs if isinstance(r, dict) and str(r.get("run_id")) == rid),
                   {"run_id": rid}) if isinstance(runs, list) else {"run_id": rid}
        op_list = poller._d(ops).get("operations")
        return trace_run(row, [o for o in op_list if isinstance(o, dict)] if isinstance(op_list, list) else [])
    runs, _ = await collect()
    hit = next((r for r in runs if r.id == run_id), None)
    if hit is None:
        raise _run_not_found()
    return hit


def _run_not_found() -> Exception:
    return human(404, "Agent run not found", "It may be older than the history Labzilla keeps.", "Go back to Agents.",
                 [("Open Agents", "/agents")])


@router.post("/agents/run", response_model=OkResponse)
async def run_agent(req: AgentRunRequest, user: User = Depends(auth.current_user)) -> OkResponse:
    item = next((c for c in catalog(user) if c.id == req.agent), None)
    if item is None:
        raise human(404, "That agent isn't installed", "Labzilla has Model Scout and the Evaluator today.",
                    "Pick one of those on the Agents page.", [("Open Agents", "/agents")], tech={"agent": req.agent})
    await auth.require(item.perm)(user)        # same 403 as every other route
    if not item.runnable:
        raise human(409, f"{item.name} can't run right now", item.why_not or "", "Change it in Settings, then retry.",
                    [("Open Settings", "/system/settings")])
    if item.id == "model-scout":
        cats = req.params.get("categories") or []
        cats = [cats] if isinstance(cats, str) else list(cats)
        bad = [c for c in cats if c not in humanize.DISCOVERY_CATEGORIES]
        if bad:
            raise human(422, "Unknown model category", f"Labzilla doesn't search for: {', '.join(bad)}.",
                        "Pick from the list.", tech={"categories": ",".join(bad)})
        try:
            r = await upstream.post("controller", "/v1/models/refresh", {"categories": cats} if cats else {},
                                    actor=user.name)
        except UpstreamError as e:
            raise upstream.to_human(e, doing="start Model Scout") from e
        auth.audit(user, "agents.run", "model-scout", {"categories": cats or "all"})
        already = (r or {}).get("status") == "already_running"
        return OkResponse(message="Model Scout is already running; follow it on the Agents page." if already else
                          "Model Scout started. Stage counts appear when the run finishes.")
    mid = req.params.get("candidate")
    if not isinstance(mid, str) or not SAFE_ID.match(mid) or ".." in mid:
        raise human(422, "Pick a candidate model", "The Evaluator needs one candidate to benchmark.",
                    "Choose a candidate from the list.")
    try:
        r = await upstream.post("controller", f"/v1/models/{mid}/benchmark", {}, actor=user.name)
    except UpstreamError as e:
        raise upstream.to_human(e, doing="start the benchmark", not_found="Candidate not found") from e
    auth.audit(user, "agents.run", "evaluator", {"model": mid})
    already = (r or {}).get("status") == "already_running"
    return OkResponse(message=f"{_model(mid)} is already being benchmarked." if already else
                      f"Benchmarking {_model(mid)}. It stops on its own if the machine needs the GPU back.")


# ── approvals ─────────────────────────────────────────────────────────────────────────────────

REVIEW_REASON = {
    "shadow_sample": "Spot check: a new version of this decision is being trialled, and a person confirms some "
                     "of its answers.",
    "shadow_disagreement": "A new version of this decision disagreed with what the agent did.",
    "below_threshold": "The decision system wasn't confident enough to decide alone.",
    "below_low": "The decision system wasn't confident enough to decide alone.",
    "flat_distribution": "The options were too close to call.",
    "exit_answer": "The decision system said it couldn't tell from the information it had.",
    "jev_unavailable": "Jev was unavailable, so a safe default was used and a person is asked instead.",
    "policy_review": "Policy requires a person to look at this kind of decision.",
}


_PLACEHOLDER = re.compile(r"`([A-Za-z_][\w]*(?:\.[\w]+)+)`")


def _state_value(state: Any, path: str) -> Any:
    """`model.id` from a flat {"model.id": …} or a nested {"model": {"id": …}} package state."""
    if isinstance(state, dict) and path in state:
        return state[path]
    node = state
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def readable_question(text: str, state: Any, data_class: str) -> str:
    """Package instructions name their inputs as `a.b` paths, which mean nothing to a person (§40).
    PUBLIC packages get the real scalar values filled in; any other class gets plain words only, so
    private inputs never reach the card."""
    def sub(m: re.Match) -> str:
        path = m.group(1)
        if data_class == "PUBLIC":
            v = _state_value(state, path)
            if isinstance(v, bool):
                return "yes" if v else "no"
            if isinstance(v, (str, int, float)) and str(v).strip():
                return f"“{str(v).strip()[:80]}”"
        return "the " + path.replace(".", " ").replace("_", " ")
    return _PLACEHOLDER.sub(sub, " ".join(text.split()))


def review_approval(t: dict[str, Any]) -> Approval:
    """A /de/human ticket → Approval. Package inputs appear only as the PUBLIC scalar values the
    question names (readable_question); the raw state is never included."""
    pkg = t.get("package") or {}
    name = str(pkg.get("decision") or str(t.get("decision_ref") or "").split("/")[0] or "decision")
    labels = [str(x) for x in pkg.get("labels") or []]
    criteria = pkg.get("criteria") if isinstance(pkg.get("criteria"), dict) else {}
    jev = pkg.get("jev") if isinstance(pkg.get("jev"), dict) else {}
    why = REVIEW_REASON.get(str(pkg.get("reason") or ""), "The decision system asked for a person's view.")
    if jev.get("answer"):
        conf = jev.get("confidence")
        why += f" Jev suggested “{jev['answer']}”" + (f" ({float(conf) * 100:.0f}% confident)."
                                                      if isinstance(conf, (int, float)) else ".")
    dc = str(pkg.get("data_class") or "CONFIDENTIAL").upper()
    instructions = readable_question(str(pkg.get("instructions") or ""), pkg.get("state"), dc)
    return Approval(
        id=f"review:{t.get('id')}", kind="review", title=f"Review a decision: {name.split('/')[0].replace('-', ' ')}",
        action=instructions[:400] or "Pick the answer you think is right.", why=why,
        impact="Nothing is waiting on this answer. It teaches the decision system (calibration).",
        options=[ApprovalOption(value=lb, label=lb.replace("_", " ").replace("-", " ").capitalize(),
                                description=readable_question(str(criteria.get(lb)), pkg.get("state"), dc)[:200]
                                if criteria.get(lb) else None)
                 for lb in labels],
        created_at=float(t.get("ts") or 0), status="pending" if t.get("status") == "pending" else "answered",
        blocking=False,
        tech=_tech(ticket=t.get("id"), decision=t.get("decision_ref"), reason=pkg.get("reason"),
                   data_class=pkg.get("data_class"), provenance=t.get("provenance_id")))


def _pairings() -> list[Approval]:
    from lif.console.routes import auth as auth_routes   # CORE owns pairing state
    try:
        return auth_routes.pending_approvals()
    except Exception as e:              # console DB unavailable: reviews still list
        log.event(LOG, "pairings_unavailable", error=type(e).__name__)
        return []


@router.get("/approvals", response_model=list[Approval])
async def approvals(user: User = Depends(auth.require("read"))) -> list[Approval]:
    out = _pairings() if "devices.manage" in user.perms else []
    try:
        q = await _get("/v1/de/human", status="pending")
    except UpstreamError as e:
        if not out:
            raise upstream.to_human(e, doing="load approvals") from e
        q = {}
    out += [review_approval(t) for t in (q or {}).get("queue") or [] if isinstance(t, dict) and t.get("status") == "pending"]
    return sorted(out, key=lambda a: (not a.blocking, -a.created_at))


@router.post("/approvals/{approval_id}", response_model=OkResponse)
async def answer(approval_id: str, req: ApprovalAnswer, user: User = Depends(auth.require("approvals.answer"))) -> OkResponse:
    kind, _, ref = approval_id.partition(":")
    if kind == "pairing":
        if req.answer not in ("approve", "reject"):
            raise human(422, "Choose Approve or Reject", "Nothing was changed.", "Pick one of the two buttons.")
        from lif.console.routes import auth as auth_routes
        p = auth_routes.decide(ref, req.answer == "approve", user)    # checks devices.manage and pending itself
        return OkResponse(message=f"{p.device_name or 'The device'} is paired." if p.status == "approved"
                          else "Pairing rejected.")
    if kind != "review" or not ref.isdigit():
        raise human(404, "Approval not found", "Nothing was changed.", "Refresh the list.", [("Retry", "retry")])
    # Re-read: the decision service lets a ticket be answered twice (the second silently overwrites).
    try:
        pending = await _get("/v1/de/human", status="pending")
    except UpstreamError as e:
        raise upstream.to_human(e, doing="answer this review") from e
    ticket = next((t for t in (pending or {}).get("queue") or [] if str(t.get("id")) == ref), None)
    if ticket is None:
        raise human(409, "This review was already answered", "Someone answered it first, or it no longer exists. "
                    "Nothing was changed.", "Refresh the list.", [("Retry", "retry")])
    labels = [str(x) for x in (ticket.get("package") or {}).get("labels") or []]
    if req.answer not in labels:
        raise human(422, "That isn't one of the options", "Nothing was changed.", "Pick one of the listed answers.",
                    tech={"options": ", ".join(labels)})
    try:
        await upstream.post("controller", f"/v1/de/human/{ref}", {"answer": req.answer}, actor=user.name)
    except UpstreamError as e:
        raise upstream.to_human(e, doing="answer this review", not_found="Review not found") from e
    auth.audit(user, "approvals.answer", f"review:{ref}", {"answer": req.answer})
    done = review_approval({**ticket, "status": "answered"})
    hub.publish("approval", done)
    return OkResponse(message="Thanks — your answer was recorded.")
