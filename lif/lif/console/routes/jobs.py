"""Jobs area: batch jobs, discovery/download/benchmark work and scheduled work in one list (spec §30, §31).

Sources, unified into contracts.Job (id = '<source>:<native id>'):
- batch service (direct, in-cluster): /v1/batch (list), /v1/batch/stats (global pause), per-job
  pause/resume and DELETE (cancel). The gateway's batch proxy drops query strings and has no
  pause/resume, so the console talks to batch itself (NetworkPolicy admits app=console).
- controller: /v1/discovery/runs (durable), /v1/benchmarks (completed only), background tasks from
  /v1/overview `tasks` (in memory, no timestamps — shown as-is, never invented).
- scheduled work: static descriptors of what the repo deploys (nightly SQLite backup CronJob, Longhorn
  daily backup) plus the weekly automatic discovery when that setting is on. No next-run times are
  invented: the cluster exposes none to the console.

Batch has no "waiting" state: a job blocked by an external workload stays queued/running and its
`reason` starts with "waiting:". The mapping below turns that into status "waiting" with a reason a
person understands ("GPU reserved for the primary workload — resumes automatically"); raw states stay in `tech`.

Pause-all goes through controller settings (`batch_paused`) so the controller stays the source of truth.
The controller computes `paused = batch_paused OR maintenance` from the request body only, so unpausing
while maintenance is on must resend maintenance=true or batch would silently resume.

Owner: AI.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from fastapi import APIRouter, Depends

from lif.common import log
from lif.console import auth, humanize, poller, upstream
from lif.console.contracts import (Job, JobCounts, JobGroups, JobsEvent, JobsResponse, JobsSummary, OkResponse,
                                   PauseAllRequest, TechDetail, User)
from lif.console.errors import human
from lif.console.events import hub
from lif.console.upstream import UpstreamError

LOG = log.get("lif.console.jobs")

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

BATCH_ID = re.compile(r"^bj_[0-9a-f]{6,32}$")
PRIORITY = {4: "Urgent", 5: "Normal", 6: "Deferrable", 7: "Experiment", 8: "Experiment"}
RECENT_LIMIT = 20
DAY = 86400.0


def _tech(**kv: Any) -> list[TechDetail]:
    return [TechDetail(label=k.replace("_", " "), value=str(v)) for k, v in kv.items() if v not in (None, "", [], {})]


# ── batch ─────────────────────────────────────────────────────────────────────────────────────

def batch_job(v: dict[str, Any]) -> Job:
    """Batch JobView (lif/batch/engine.py) → Job."""
    state = str(v.get("state") or v.get("status") or "")
    reason = str(v.get("reason") or "")
    c = v.get("counts") or {}
    total = int(v.get("total") or 0)
    ok, bad = int(c.get("succeeded") or 0), int(c.get("failed") or 0)
    status, label, resumes = humanize.job_state(state, reason)
    why = humanize.job_reason(reason)
    if state == "completed" and bad:
        label = f"Finished with {bad} failure{'s' * (bad != 1)}"
    elif state == "failed":
        why = why or "Every item failed."
    if state in ("queued", "running"):
        actions = ["pause", "cancel"]
    elif state == "paused":
        actions = ["resume", "cancel"]
    else:
        actions = []
    pr = v.get("priority")
    model = v.get("model") or "local/batch"
    title = (v.get("description") or "").strip() or f"Batch of {total} request{'s' * (total != 1)}"
    return Job(id=f"batch:{v.get('id')}", kind="batch", title=title[:120], status=status, status_label=label,  # type: ignore[arg-type]
               reason=why, resumes_automatically=resumes,
               progress=float(v["progress"]) if total and v.get("progress") is not None else None,
               counts=JobCounts(done=ok, total=total, failed=bad) if total else None,
               started_at=v.get("created"), finished_at=v.get("finished"), owner=v.get("owner"),
               actions=actions,  # type: ignore[arg-type]
               tech=_tech(batch_id=v.get("id"), state=state, reason=reason, model=model,
                          priority=f"P{pr} {PRIORITY.get(pr, '')} ({v.get('priority_source') or 'default'})"
                          if pr else None, data_class=v.get("data_class"), item_counts=c or None))


# ── controller work ───────────────────────────────────────────────────────────────────────────

def _model_label(mid: str) -> str:
    from lif.console.routes.ai import friendly_model   # one naming rule for models everywhere
    return friendly_model(mid) or mid


def discovery_job(run: dict[str, Any], running_id: Any) -> Job:
    funnel = run.get("funnel") or {}
    cats = list((funnel.get("categories") or {}).keys())
    shortlisted = sum(len((c or {}).get("shortlisted") or []) for c in (funnel.get("categories") or {}).values()
                      if isinstance(c, dict))
    st = run.get("status")
    why = None
    # A row still 'running' that the controller isn't running (another run, or none after a restart) is stale.
    if st == "running" and running_id != run.get("id"):
        status, label, why = "failed", "Interrupted", "The controller restarted during this run, so it never finished."
    elif st == "running":
        status, label = "running", "Searching Hugging Face"
    elif st == "succeeded":
        status, label = "completed", (f"Finished — {shortlisted} candidate{'s' * (shortlisted != 1)} shortlisted"
                                      if shortlisted else "Finished — no better models found")
    else:
        status, label, why = "failed", "Failed", humanize.run_error(run.get("error"))
    title = "Check for better models" + (f" ({', '.join(cats)})" if cats and len(cats) < 6 else "")
    return Job(id=f"discovery:{run.get('id')}", kind="discovery", title=title, status=status, status_label=label,  # type: ignore[arg-type]
               reason=why, started_at=run.get("ts"), finished_at=run.get("finished"), owner="Model Scout",
               tech=_tech(run_id=run.get("id"), raw_status=st, total_ms=funnel.get("total_ms"), error=run.get("error")))


def task_job(key: str, value: str) -> Job | None:
    """Controller background task ('download:<mid>' | 'benchmark:<mid>' → 'running' | 'done' | 'failed: …')."""
    kind, _, mid = key.partition(":")
    if kind not in ("download", "benchmark") or not mid:
        return None
    verb = "Download" if kind == "download" else "Benchmark"
    if value == "running":
        status, label, why = "running", "Running", None
    elif value == "done":
        status, label, why = "completed", "Finished", "Finished since the controller last started (no time recorded)."
    else:
        status, label, why = "failed", "Failed", value.partition(":")[2].strip()[:200] or "The task failed."
    return Job(id=f"{kind}:{mid}", kind=kind, title=f"{verb} {_model_label(mid)}", status=status,  # type: ignore[arg-type]
               status_label=label, reason=why, owner="Evaluator" if kind == "benchmark" else None,
               tech=_tech(task=key, raw_status=value))


def benchmark_job(b: dict[str, Any]) -> Job:
    s = b.get("summary") or {}
    q = s.get("quality")
    label = f"Finished — quality {q * 100:.0f}%" if isinstance(q, (int, float)) else "Finished"
    if s.get("errors"):
        label += f", {s['errors']} error{'s' * (s['errors'] != 1)}"
    return Job(id=f"benchmark:{b.get('model')}:{b.get('id')}", kind="benchmark",
               title=f"Benchmark {_model_label(str(b.get('model')))}", status="completed", status_label=label,
               finished_at=b.get("ts"), owner="Evaluator",
               tech=_tech(model=b.get("model"), suite=b.get("suite"), benchmark_id=b.get("id")))


def scheduled_jobs(ctl_settings: dict[str, Any]) -> list[Job]:
    """What the repo schedules (deploy/k8s/base/15-backup.yaml, homelab/longhorn/recurring-jobs.yaml) and the
    controller's weekly automatic discovery. Status 'queued' + a 'Scheduled' label; never counted as queued work."""
    out = [
        Job(id="scheduled:sqlite-backup", kind="backup", title="Nightly database backup", status="queued",
            status_label="Scheduled · every night at 03:45",
            reason="Copies LIF's SQLite databases (models, decisions) to MinIO. Times are cluster time.",
            tech=_tech(cronjob="ai-system/lif-sqlite-backup", schedule="45 3 * * *")),
        Job(id="scheduled:longhorn-backup", kind="backup", title="Nightly volume backup", status="queued",
            status_label="Scheduled · every night at 03:15",
            reason="Backs up Longhorn volumes to MinIO. Times are cluster time.",
            tech=_tech(recurring_job="longhorn-system/backup-daily", schedule="15 3 * * *")),
    ]
    if ctl_settings.get("automatic_discovery"):
        paused = ctl_settings.get("maintenance") or ctl_settings.get("discovery_disabled")
        out.append(Job(
            id="scheduled:auto-discovery", kind="discovery", title="Weekly check for better models",
            status="paused" if paused else "queued", status_label="Paused" if paused else "Scheduled · weekly",
            reason=("Maintenance mode is on." if ctl_settings.get("maintenance") else
                    "Model discovery is switched off.") if paused else
            "Model Scout runs on its own about once a week; nothing is downloaded without your review.",
            owner="Model Scout", tech=_tech(setting="automatic_discovery")))
    return out


# ── aggregation ───────────────────────────────────────────────────────────────────────────────

async def _fetch(svc: Any, path: str, **params: Any) -> Any:
    return await upstream.get(svc, path, params=params or None)


async def collect() -> tuple[list[Job], bool, list[str]]:
    """Every job the console can see → (jobs, batch paused, notes about sources that didn't answer)."""
    snap = poller.snapshot()
    overview = snap.raw.get("overview") if isinstance(snap.raw.get("overview"), dict) else None
    ctl_settings = snap.raw.get("settings") if isinstance(snap.raw.get("settings"), dict) else None
    calls = {"batch": _fetch("batch", "/v1/batch", limit=100), "stats": _fetch("batch", "/v1/batch/stats"),
             "runs": _fetch("controller", "/v1/discovery/runs"), "bench": _fetch("controller", "/v1/benchmarks")}
    if overview is None:
        calls["overview"] = _fetch("controller", "/v1/overview")
    if ctl_settings is None:
        calls["settings"] = _fetch("controller", "/v1/settings")
    got = dict(zip(calls, await asyncio.gather(*calls.values(), return_exceptions=True)))
    ok = {k: v for k, v in got.items() if not isinstance(v, BaseException)}
    notes = []
    if "batch" not in ok:
        notes.append("The batch service didn't answer, so batch jobs aren't shown.")
    if "runs" not in ok and "overview" not in ok and overview is None:
        notes.append("The controller didn't answer, so model work isn't shown.")
    overview = overview or ok.get("overview") or {}
    ctl_settings = ctl_settings or ok.get("settings") or {}
    jobs: list[Job] = [batch_job(v) for v in (ok.get("batch") or {}).get("data") or [] if isinstance(v, dict)]
    runs = ok.get("runs") or {}
    jobs += [discovery_job(r, runs.get("running")) for r in (runs.get("runs") or [])[:10] if isinstance(r, dict)]
    benches = [b for b in (ok.get("bench") or {}).get("benchmarks") or [] if isinstance(b, dict)][:10]
    benched = {str(b.get("model")) for b in benches}
    for key, value in (overview.get("tasks") or {}).items():
        j = task_job(str(key), str(value))
        # a finished benchmark task duplicates the stored result, which has the real time
        if j and not (j.kind == "benchmark" and j.status == "completed" and key.partition(":")[2] in benched):
            jobs.append(j)
    jobs += [benchmark_job(b) for b in benches]
    jobs += scheduled_jobs(ctl_settings)
    stats = ok.get("stats")
    paused = bool(stats.get("paused")) if isinstance(stats, dict) else snap.status.batch_paused
    return jobs, paused, notes


def _when(j: Job) -> float:
    return j.finished_at or j.started_at or 0.0


def group(jobs: list[Job]) -> tuple[JobsSummary, JobGroups]:
    now = time.time()
    g = JobGroups()
    for j in jobs:
        {"running": g.running, "queued": g.queued, "waiting": g.waiting, "paused": g.waiting,
         "completed": g.completed, "cancelled": g.completed, "failed": g.failed}[j.status].append(j)
    for lst in (g.running, g.queued, g.waiting):
        lst.sort(key=lambda j: (j.id.startswith("scheduled:"), -(j.started_at or 0)))
    g.completed = sorted(g.completed, key=_when, reverse=True)[:RECENT_LIMIT]
    g.failed = sorted(g.failed, key=_when, reverse=True)[:RECENT_LIMIT]
    real = [j for j in jobs if not j.id.startswith("scheduled:")]
    summary = JobsSummary(running=sum(j.status == "running" for j in real),
                          queued=sum(j.status == "queued" for j in real),
                          waiting=sum(j.status in ("waiting", "paused") for j in real),
                          failed_24h=sum(j.status == "failed" and _when(j) >= now - DAY for j in real))
    return summary, g


GROUPS = ("running", "queued", "waiting", "completed", "failed")


@router.get("", response_model=JobsResponse)
async def list_jobs(status: str | None = None, user: User = Depends(auth.require("read"))) -> JobsResponse:
    jobs, paused, notes = await collect()
    summary, groups = group(jobs)
    if status in GROUPS:
        groups = JobGroups(**{status: getattr(groups, status)})
    return JobsResponse(summary=summary, groups=groups, batch_paused=paused, note=" ".join(notes) or None)


# Registered before /{job_id}/{action} so 'batch/pause-all' never reaches the per-job handler.
@router.post("/batch/pause-all", response_model=OkResponse)
async def pause_all(req: PauseAllRequest, user: User = Depends(auth.require("jobs.control"))) -> OkResponse:
    try:
        current = await upstream.get("controller", "/v1/settings")
    except UpstreamError as e:
        raise upstream.to_human(e, doing="pause batch work") from e
    body: dict[str, Any] = {"batch_paused": req.paused}
    message = "Batch work paused. Items already running will finish." if req.paused else "Batch work resumed."
    if not req.paused and (current or {}).get("maintenance"):
        body["maintenance"] = True      # keep the OR: the controller would otherwise unpause batch
        poller.note_echo("maintenance")
        message = "Batch is no longer paused on its own, but maintenance mode is on, so it stays paused until " \
                  "maintenance ends."
    try:
        await upstream.post("controller", "/v1/settings", body, actor=user.name)
    except UpstreamError as e:
        raise upstream.to_human(e, doing="pause batch work" if req.paused else "resume batch work") from e
    auth.audit(user, "jobs.batch.pause_all" if req.paused else "jobs.batch.resume_all", "batch",
               {"maintenance_kept": "maintenance" in body})
    hub.publish("jobs", JobsEvent(summary=poller.snapshot().jobs))
    poller.refresh_soon()               # Home's "batch paused" follows now, not on the next cycle
    return OkResponse(message=message)


async def find(job_id: str) -> Job | None:
    if job_id.startswith("batch:"):
        bid = job_id.partition(":")[2]
        if not BATCH_ID.match(bid):
            return None
        try:
            return batch_job(await upstream.get("batch", f"/v1/batch/{bid}"))
        except UpstreamError as e:
            if e.status == 404:
                return None
            raise upstream.to_human(e, doing="read this job", not_found="Job not found") from e
    jobs, _, _ = await collect()
    return next((j for j in jobs if j.id == job_id), None)


@router.get("/{job_id}", response_model=Job)
async def get_job(job_id: str, user: User = Depends(auth.require("read"))) -> Job:
    j = await find(job_id)
    if j is None:
        raise human(404, "Job not found", "It may have finished long ago or been removed.", "Go back to Jobs.",
                    [("Open Jobs", "/jobs")])
    return j


ACTION_VERB = {"pause": "paused", "resume": "resumed", "cancel": "cancelled", "retry": "retried"}


@router.post("/{job_id}/{action}", response_model=Job)
async def job_action(job_id: str, action: str, user: User = Depends(auth.require("jobs.control"))) -> Job:
    if action not in ACTION_VERB:
        raise human(404, "That action isn't available", "Nothing was changed.", "Use the buttons on the job.")
    if not job_id.startswith("batch:"):
        raise human(409, f"This job can't be {ACTION_VERB[action]}",
                    "Only batch jobs can be paused, resumed or cancelled. Model work runs to completion "
                    "(and stops on its own if the machine needs the GPU back).", "Nothing was changed.",
                    tech={"job": job_id, "action": action})
    job = await find(job_id)
    if job is None:
        raise human(404, "Job not found", "It may have been removed.", "Refresh Jobs.", [("Open Jobs", "/jobs")])
    if action not in job.actions:
        state = job.status_label.lower() or job.status
        raise human(409, f"This job can't be {ACTION_VERB[action]} right now", f"It is {state}. Nothing was changed.",
                    "Refresh to see the current state.", [("Retry", "retry")], tech={"job": job_id, "action": action})
    bid = job_id.partition(":")[2]
    try:
        if action == "cancel":     # in-flight items finish and keep their results
            view = await upstream.delete("batch", f"/v1/batch/{bid}", actor=user.name)
        else:
            view = await upstream.post("batch", f"/v1/batch/{bid}/{action}", None, actor=user.name)
    except UpstreamError as e:
        raise upstream.to_human(e, doing=f"{action} this job", not_found="Job not found") from e
    out = batch_job(view) if isinstance(view, dict) and view.get("id") else job
    auth.audit(user, f"jobs.{action}", job_id)
    hub.publish("jobs", JobsEvent(summary=poller.snapshot().jobs, job=out))
    return out
