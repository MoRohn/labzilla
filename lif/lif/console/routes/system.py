"""System area: /api/system/{status,compute,timeline,services,storage,logs,alerts,settings} (spec §35–§38, §80, §82).

Reads come from the poller snapshot or from one allowlisted upstream call each, and degrade instead of
failing: an unreachable upstream gives cached data or an honest empty state with a reason, never a
500 (§60, §81). Mutations check the permission server-side, audit, and refresh the poller.

Owner: SYS.
"""
from __future__ import annotations

import math
import time
from typing import Any

from fastapi import APIRouter, Depends, Query

from lif.console import auth, earn, poller, settings, upstream
from lif.console import humanize as hz
from lif.console.contracts import (Alert, AlertsResponse, ComputeView, LogsResponse, ServiceHealth, SettingItem,
                                   SettingKey, SettingsView, SettingUpdate, StorageItem, StorageSummary, SystemStatus,
                                   TimelineBucket, TimelineResponse, User)
from lif.console.errors import human
from lif.console.upstream import UpstreamError

router = APIRouter(prefix="/api/system", tags=["system"])

_read = Depends(auth.require("read"))


@router.get("/status", response_model=SystemStatus)
async def status(_: User = _read) -> SystemStatus:
    """Home's status block, from the poller cache (never waits on an upstream)."""
    poller.ensure_fresh()
    return poller.snapshot().status


@router.get("/compute", response_model=ComputeView)
async def compute(_: User = _read) -> ComputeView:
    poller.ensure_fresh()
    return poller.snapshot().compute


@router.get("/services", response_model=list[ServiceHealth])
async def services(_: User = _read) -> list[ServiceHealth]:
    poller.ensure_fresh()
    return poller.snapshot().services


@router.post("/services/{key}/retry", response_model=ServiceHealth)
async def retry_service(key: str, user: User = Depends(auth.require("system.safe"))) -> ServiceHealth:
    """Re-probe now (§82 'Retry'). Restart is not offered: no upstream supports it without cluster access."""
    if key not in {s.key for s in poller.snapshot().services} | set(hz.SERVICE_NAMES):
        raise human(404, "Unknown service", "There's no service with that name.", "Go back to System → Services.")
    auth.audit(user, "service.retry", key)
    snap = await poller.refresh()
    found = next((s for s in snap.services if s.key == key), None)
    if found is None:
        raise human(404, "Unknown service", "There's no service with that name.", "Go back to System → Services.")
    return found


# ── workload timeline (§37) ──────────────────────────────────────────────────────────────────

# Each query is evaluated at the end of every hour and covers the hour before it ([1h:1m] subquery).
TIMELINE_QUERIES: dict[str, str] = {
    # gpusched_leases is an unlabelled count of live GPU leases (no per-class label is exported). LIF holds
    # none today (CPU tiers only), so any lease is the primary workload's.
    "blerbz": "avg_over_time((max(gpusched_leases) > bool 0)[1h:1m])",
    "blerbz_fallback": "avg_over_time((max(lif_blerbz_state) >= bool 3)[1h:1m])",
    "ai": "avg_over_time((sum(lif_inflight_requests) > bool 0)[1h:1m])",
    # controller availability probes are real inference every 30 s; they are not "work". The gateway labels
    # them with the X-LIF-Workload header the controller sends ("availability-probe", controller/app.py).
    "tasks": 'sum(increase(lif_tasks_total{workload!~"availability-probe|controller-probe"}[1h]))',
    "batch": "sum(increase(lif_batch_items_total[1h]))",
}


def _series(result: list[dict[str, Any]]) -> dict[int, float]:
    out: dict[int, float] = {}
    for s in result or []:
        for pair in (s.get("values") or []) if isinstance(s, dict) else []:
            if isinstance(pair, list) and len(pair) == 2 and hz.num(pair[1]) is not None:
                out[int(round(float(pair[0])))] = hz.num(pair[1])  # type: ignore[assignment]
    return out


def bucket_label(blerbz_pct: float | None, ai_pct: float | None, tasks: float | None, batch: float | None) -> str:
    parts = []
    if blerbz_pct is not None and blerbz_pct >= 10:
        parts.append("BLERBZ video")
    if (tasks or 0) >= 1 or (ai_pct or 0) >= 5:
        parts.append("inference")
    if (batch or 0) >= 1:
        parts.append("batch")
    return " + ".join(parts) or "idle"


@router.get("/timeline", response_model=TimelineResponse)
async def timeline(hours: int = Query(12, ge=1, le=168), _: User = _read) -> TimelineResponse:
    """Hourly workload buckets from Prometheus history (retention about a week); honest when unavailable."""
    # Whole clock hours (08:00–09:00, §37) ending at the last completed hour, so labels read as times of day.
    end = math.floor(time.time() / 3600) * 3600
    start = end - (hours - 1) * 3600
    try:
        blerbz = _series(await upstream.prom_range(TIMELINE_QUERIES["blerbz"], start, end, 3600, strict=True))
    except UpstreamError as e:
        return TimelineResponse(hours=hours, available=False,
                                reason=f"{upstream.reason(e)}, so the workload history can't be shown.")
    if not blerbz:
        blerbz = _series(await upstream.prom_range(TIMELINE_QUERIES["blerbz_fallback"], start, end, 3600))
    ai = _series(await upstream.prom_range(TIMELINE_QUERIES["ai"], start, end, 3600))
    tasks = _series(await upstream.prom_range(TIMELINE_QUERIES["tasks"], start, end, 3600))
    batch = _series(await upstream.prom_range(TIMELINE_QUERIES["batch"], start, end, 3600))
    if not (blerbz or ai or tasks or batch):
        return TimelineResponse(hours=hours, available=False,
                                reason="No workload history has been recorded for this period yet.")
    buckets = []
    for i in range(hours):
        t = start + i * 3600
        b = blerbz.get(t)
        a = ai.get(t)
        bp = round(b * 100, 1) if b is not None else None
        ap = round(a * 100, 1) if a is not None else None
        n = batch.get(t)
        buckets.append(TimelineBucket(start=t - 3600, end=t, label=bucket_label(bp, ap, tasks.get(t), n),
                                      blerbz_pct=bp, ai_pct=ap, batch_items=int(round(n)) if n is not None else None))
    return TimelineResponse(hours=hours, buckets=buckets)


# ── storage ─────────────────────────────────────────────────────────────────────────────────

STORAGE_GROUPS: dict[str, str] = {"production": "Models in use", "rollback": "Models kept for rollback",
                                  "candidate": "Models under evaluation", "failed": "Removable models (failed or rejected)",
                                  "metadata_only": "Models found but not downloaded"}


@router.get("/storage", response_model=StorageSummary)
async def storage(_: User = _read) -> StorageSummary:
    items: list[StorageItem] = []
    notes: list[str] = []
    try:
        groups = await upstream.get("controller", "/v1/storage")
        for key, label in STORAGE_GROUPS.items():
            g = groups.get(key) if isinstance(groups, dict) else None
            if not isinstance(g, dict):
                continue
            b = hz.num(g.get("bytes"))
            models = g.get("models") if isinstance(g.get("models"), list) else []
            items.append(StorageItem(
                key=f"models:{key}", label=label, health="healthy", used_gb=round(b / 2 ** 30, 1) if b else None,
                count=int(hz.num(g.get("count")) or 0),
                note="From the model registry, not a disk measurement" + ("" if b else "; size not recorded"),
                tech=hz.tech(retention_class=key, models=", ".join(str(m) for m in models[:20]))))
    except UpstreamError as e:
        notes.append(f"{upstream.reason(e)}: model storage can't be listed.")
    disk_size = await upstream.prom('max(node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"})')
    disk_free = await upstream.prom('max(node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"})')
    size, free = upstream.prom_value(disk_size), upstream.prom_value(disk_free)
    if size and free is not None:
        frac = free / size
        items.append(StorageItem(key="disk:root", label="Root disk", used_gb=round((size - free) / 2 ** 30, 1),
                                 total_gb=round(size / 2 ** 30, 1),
                                 health="attention" if frac < 0.1 else "degraded" if frac < 0.2 else "healthy",
                                 note=f"{frac * 100:.0f}% free", tech=hz.tech(source="node exporter", mountpoint="/")))
    else:
        notes.append("Disk usage needs Prometheus, which isn't answering.")
    vols = await upstream.prom('kubelet_volume_stats_used_bytes{namespace=~"ai-system|ai-batch|ai-serving|earn"}')
    caps = await upstream.prom('kubelet_volume_stats_capacity_bytes{namespace=~"ai-system|ai-batch|ai-serving|earn"}')
    # Keyed by namespace too: two namespaces may each have a PVC with the same name.
    cap_by = {((s.get("metric") or {}).get("namespace"), (s.get("metric") or {}).get("persistentvolumeclaim")):
              upstream.prom_value([s]) for s in caps}
    for s in vols:
        pvc = (s.get("metric") or {}).get("persistentvolumeclaim")
        used, total = upstream.prom_value([s]), cap_by.get(((s.get("metric") or {}).get("namespace"), pvc))
        if not pvc or used is None:
            continue
        frac = (total - used) / total if total else None
        items.append(StorageItem(key=f"volume:{pvc}", label=f"Volume {pvc}", used_gb=round(used / 2 ** 30, 2),
                                 total_gb=round(total / 2 ** 30, 2) if total else None,
                                 health="unknown" if frac is None else "attention" if frac < 0.1 else
                                 "degraded" if frac < 0.2 else "healthy",
                                 tech=hz.tech(namespace=(s.get("metric") or {}).get("namespace"), pvc=pvc)))
    db = settings.db_path()
    try:
        if db.is_file():
            items.append(StorageItem(key="console:db", label="Console database", health="healthy",
                                     used_gb=round(db.stat().st_size / 2 ** 30, 3), note="Users, devices and Ask history"))
    except OSError:
        pass
    return StorageSummary(items=items, note=" ".join(notes) or None)


# ── logs (§80): relevant events first, raw logs honest ───────────────────────────────────────

RAW_LOGS_NOTE = ("Raw service logs aren't available in the console yet. These are the relevant events "
                 "Labzilla records; service logs stay on the machine.")
_LEVELS = {"error": {"error"}, "warning": {"error", "warning"}, "all": {"error", "warning", "info", "success"}}


@router.get("/logs", response_model=LogsResponse)
async def logs(level: str = Query("warning", pattern="^(error|warning|all)$"), q: str = Query("", max_length=200),
               limit: int = Query(200, ge=1, le=1000), _: User = _read) -> LogsResponse:
    note = RAW_LOGS_NOTE
    try:
        body = await upstream.get("controller", "/v1/activity", params={"limit": 1000})
        rows = body.get("activity") if isinstance(body, dict) else None
        events = [e for e in (hz.activity(r) for r in rows or [] if isinstance(r, dict)) if e is not None]
    except UpstreamError as e:
        events = list(poller.snapshot().activity)
        note = f"{upstream.reason(e)}; showing the most recent cached events. {RAW_LOGS_NOTE}"
    # Earn activity (System → Logs) comes from the poller's cached earn status, independent of the controller.
    earn_st = poller.snapshot().raw.get("earn")
    if isinstance(earn_st, dict):
        events = sorted([*events, *earn.events(earn_st)], key=lambda e: e.ts, reverse=True)
    elif settings.earn_url():
        note = f"Earn's activity can't be read right now. {note}"
    allowed = _LEVELS[level]
    needle = q.strip().lower()

    def match(e: Any) -> bool:
        hay = " ".join([e.title, e.detail or "", *(t.value for t in e.tech)]).lower()
        return e.severity in allowed and (not needle or needle in hay)

    return LogsResponse(events=[e for e in events if match(e)][:limit], raw_logs_available=False, raw_logs_note=note)


# ── alerts ──────────────────────────────────────────────────────────────────────────────────

@router.get("/alerts", response_model=AlertsResponse)
async def alerts(_: User = _read) -> AlertsResponse:
    try:
        firing = await upstream.prom('ALERTS{alertstate="firing"}', strict=True)
    except UpstreamError as e:
        return AlertsResponse(available=False, reason=f"{upstream.reason(e)}, so alerts can't be read.")
    since = {}
    for s in await upstream.prom("ALERTS_FOR_STATE"):
        m = s.get("metric") or {}
        since[(m.get("alertname"), m.get("instance"), m.get("namespace"))] = upstream.prom_value([s])
    out: list[Alert] = []
    for s in firing:
        m = s.get("metric") if isinstance(s, dict) else None
        if not isinstance(m, dict) or m.get("alertname") in hz.IGNORED_ALERTS:
            continue
        name = str(m.get("alertname") or "")
        out.append(Alert(id=":".join(str(m.get(k) or "") for k in ("alertname", "namespace", "instance", "pod")),
                         name=name, severity=hz.alert_severity(m.get("severity")), summary=hz.alert_summary(name),
                         since=since.get((m.get("alertname"), m.get("instance"), m.get("namespace"))),
                         tech=hz.tech(alertname=name, severity=m.get("severity"), namespace=m.get("namespace"),
                                      pod=m.get("pod"), instance=m.get("instance"))))
    order = {"error": 0, "warning": 1, "info": 2, "success": 3}
    return AlertsResponse(alerts=sorted(out, key=lambda a: order[a.severity]))


# ── settings (operator switches; consequence stated before flipping, §110) ───────────────────

# key → (label, description, consequence when turned on / set, kind, unit, perm, dangerous)
SETTINGS: dict[str, tuple[str, str, str, str, str | None, str, bool]] = {
    "maintenance": ("Maintenance mode", "For planned work on the machine.",
                    "Batch work pauses, automatic downloads and promotions stop, and benchmarks of models that "
                    "aren't running are refused until it's turned off.", "toggle", None, "system.settings", False),
    "batch_paused": ("Pause batch jobs", "Holds all background batch work.",
                     "No new batch items start; queued work is kept and resumes when unpaused.", "toggle", None,
                     "jobs.control", False),
    "automatic_discovery": ("Automatic model checks", "Checks Hugging Face for better models about once a week.",
                            "A model check runs on a schedule; each check uses a small amount of paid Jev screening.",
                            "toggle", None, "system.settings", False),
    "automatic_download": ("Automatic downloads", "Downloads shortlisted candidates without asking.",
                           "Candidates download on their own, using disk space and bandwidth.", "toggle", None,
                           "system.settings", False),
    "automatic_promotion": ("Automatic promotion", "Lets Labzilla promote a model that passed evaluation.",
                            "A role can switch to a new model without asking; the previous model stays available "
                            "for rollback.", "toggle", None, "system.settings", True),
    "discovery_disabled": ("Turn off model checks", "Stops every model check, including ones you start.",
                           "'Check for better models' will do nothing until this is turned off.", "toggle", None,
                           "system.settings", False),
    "jev_disabled": ("Turn off Jev decisions", "Decisions use built-in rules only.",
                     "Nothing is sent to Jev; routing and reviews use rules, which can be less accurate.", "toggle",
                     None, "system.settings", False),
    "reserve_gpu_mib": ("Extra memory reserve", "Memory kept free for BLERBZ on top of the normal safety margin.",
                        "The memory guard pauses optional AI services earlier, so local AI has less redundancy.",
                        "number", "MiB", "system.settings", False),
}
RESERVE_MAX_MIB = 65536


def _view(values: dict[str, Any]) -> SettingsView:
    items = []
    for key, (label, desc, cons, kind, unit, perm, dangerous) in SETTINGS.items():
        v = values.get(key)
        value = (hz.num(v) if kind == "number" else bool(v)) if v is not None else None
        items.append(SettingItem(key=key, label=label, description=desc, consequence=cons, kind=kind,  # type: ignore[arg-type]
                                 value=value, unit=unit, perm=perm, dangerous=dangerous))  # type: ignore[arg-type]
    return SettingsView(items=items)


@router.get("/settings", response_model=SettingsView)
async def get_settings(_: User = _read) -> SettingsView:
    try:
        values = await upstream.get("controller", "/v1/settings")
        return _view(values if isinstance(values, dict) else {})
    except UpstreamError as e:
        cached = poller.snapshot().raw.get("settings") or {}
        view = _view(cached)
        view.available = False
        view.reason = (f"{upstream.reason(e)}; showing the last known values. Changes can't be saved right now."
                       if cached else f"{upstream.reason(e)}, so settings can't be read right now.")
        return view


def setting_perm(key: SettingKey) -> str:
    return SETTINGS[key][5]


@router.post("/settings", response_model=SettingsView)
async def update_setting(req: SettingUpdate, user: User = Depends(auth.current_user)) -> SettingsView:
    """Change one switch. batch_paused needs jobs.control; everything else needs system.settings."""
    await auth.require(setting_perm(req.key))(user)  # type: ignore[arg-type]
    kind = SETTINGS[req.key][3]
    if kind == "toggle" and not isinstance(req.value, bool):
        raise human(422, "That setting is on or off", "Nothing was changed.", "Choose on or off.")
    if kind == "number":
        v = hz.num(req.value)
        if isinstance(req.value, bool) or v is None or v != int(v) or v < 0 or v > RESERVE_MAX_MIB:
            raise human(422, "That reserve isn't valid", "Nothing was changed.",
                        f"Enter a whole number of MiB between 0 and {RESERVE_MAX_MIB}.")
        value: Any = int(v)
    else:
        value = req.value
    try:
        out = await poller.set_setting(req.key, value, actor=user.name)
    except UpstreamError as e:
        raise upstream.to_human(e, doing=f"change {SETTINGS[req.key][0].lower()}") from None
    auth.audit(user, "settings.update", req.key, {"value": value})
    return _view(out)
