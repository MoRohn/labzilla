"""Primary-workload capacity state and CAN_RUN admission, derived from gpusched (read-only).

gpusched is the GPU admission authority on this host. The LIF never allocates GPU
memory on its own; it reads gpusched's /metrics (metrics token = GET /metrics only) to
decide how politely its CPU tiers and batch work must behave.

    IMMINENT  a production lease is live, a resident model is not loaded (reloading),
              gpusched is unreachable/stale, or it reports holds      → fail-safe
    HIGH      P(production arrival in the next hour) ≥ primary_workload.state_thresholds.high
    MODERATE  P ≥ moderate
    LOW       otherwise
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import asdict, dataclass, field
from enum import IntEnum

import httpx

from lif.common import config, log, metrics
from lif.gpu import workload

LOG = log.get("lif.gpu")


class BlerbzState(IntEnum):
    LOW = 0
    MODERATE = 1
    HIGH = 2
    IMMINENT = 3


class Verdict:
    RUN = "RUN"
    RUN_DEGRADED = "RUN_DEGRADED"
    QUEUE = "QUEUE"
    PAUSE_BACKGROUND = "PAUSE_BACKGROUND"
    EVICT_MODEL = "EVICT_MODEL"
    REJECT = "REJECT"
    OPTIONAL_EXTERNAL_FALLBACK = "OPTIONAL_EXTERNAL_FALLBACK"


# Spec priority classes → gpusched class for anything that needs the GPU.
PRIORITY_TO_GPUSCHED = {0: "production", 1: "production", 2: "interactive", 3: "interactive",
                        4: "background", 5: "background", 6: "background", 7: "background", 8: "background"}

_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)')
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def parse_prometheus(text: str) -> list[tuple[str, dict, float]]:
    out = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        labels = dict(_LABEL.findall(m.group(2) or ""))
        try:
            out.append((m.group(1), labels, float(m.group(3))))
        except ValueError:
            continue
    return out


@dataclass
class Snapshot:
    ts: float = 0.0
    reachable: bool = False
    production_live: bool = False
    production_leases: int = 0
    background_leases: int = 0
    p_next_hour: float | None = None
    forecast_authoritative: bool = False
    admissible_mib: float = 0.0
    mem_available_mib: float = 0.0
    gpu_util_percent: float = 0.0
    holds: int = 0
    residents_loaded: dict[str, bool] = field(default_factory=dict)
    state: BlerbzState = BlerbzState.IMMINENT
    reason: str = "no data yet"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.name
        d["age_sec"] = round(time.time() - self.ts, 1) if self.ts else None
        return d


def standalone_snapshot() -> Snapshot:
    """What Labzilla reports when no external workload is configured: quiet, not fail-safe."""
    return Snapshot(ts=time.time(), reachable=True, state=BlerbzState.LOW,
                    reason="standalone (no external workload configured)")


def derive_state(snap: Snapshot, thresholds: dict | None = None) -> tuple[BlerbzState, str]:
    t = thresholds or workload.thresholds()
    if not snap.reachable:
        return BlerbzState.IMMINENT, "gpusched unreachable (fail-safe)"
    if snap.production_live:
        return BlerbzState.IMMINENT, f"{snap.production_leases} production lease(s) live"
    not_loaded = [n for n, ok in snap.residents_loaded.items() if not ok]
    if not_loaded:
        return BlerbzState.IMMINENT, f"resident(s) reloading: {', '.join(not_loaded)}"
    p = snap.p_next_hour
    if p is None:
        return BlerbzState.MODERATE, "no forecast"
    if p >= float(t.get("high", 0.5)):
        return BlerbzState.HIGH, f"P(production within 1h) = {p:.0%}"
    if p >= float(t.get("moderate", 0.2)):
        return BlerbzState.MODERATE, f"P(production within 1h) = {p:.0%}"
    return BlerbzState.LOW, f"P(production within 1h) = {p:.0%}"


def snapshot_from_metrics(text: str) -> Snapshot:
    s = Snapshot(ts=time.time(), reachable=True)
    for name, labels, v in parse_prometheus(text):
        if name == "gpusched_leases":
            k = labels.get("klass")
            if k == "production":
                s.production_leases += int(v)
            elif k:
                s.background_leases += int(v)
        elif name == "gpusched_forecast_p_arrival_next_hour":
            s.p_next_hour = v
        elif name == "gpusched_forecast_authoritative":
            s.forecast_authoritative = v >= 1
        elif name == "gpusched_capacity_admissible_mib":
            s.admissible_mib = v
        elif name == "gpusched_mem_available_mib":
            s.mem_available_mib = v
        elif name == "gpusched_gpu_util_percent":
            s.gpu_util_percent = v
        elif name == "gpusched_holds":
            s.holds = int(v)
        elif name == "gpusched_resident_loaded" and "resident" in labels:
            s.residents_loaded[labels["resident"]] = v >= 1
        elif name == "gpusched_up" and v < 1:
            s.reachable = False
    s.production_live = s.production_leases > 0
    s.state, s.reason = derive_state(s)
    return s


class GpuStateWatcher:
    """Polls gpusched every poll_sec. `current()` is always safe to call; staleness
    degrades the state to IMMINENT rather than trusting old data."""

    def __init__(self, url: str | None = None, token: str | None = None, client: httpx.AsyncClient | None = None):
        c = config.get("gpusched") or {}
        self.url = (url or c.get("url", "http://10.42.0.1:8770")).rstrip("/")
        # $LIF_GPUSCHED_TOKEN_FILE lets host-side tools use a readable copy; in the cluster it is unset.
        self.token = token if token is not None else _read_token(os.environ.get("LIF_GPUSCHED_TOKEN_FILE")
                                                                 or c.get("token_file"))
        self.poll = float(c.get("poll_sec", 5))
        self.stale = float(c.get("stale_after_sec", 30))
        self.client = client or httpx.AsyncClient(timeout=3)
        self._snap = Snapshot()
        self._task: asyncio.Task | None = None

    async def refresh(self) -> Snapshot:
        if not workload.enabled():
            snap = standalone_snapshot()
            self._snap = snap
            metrics.blerbz_state.set(int(snap.state))
            return snap
        try:
            r = await self.client.get(f"{self.url}/metrics", headers={"Authorization": f"Bearer {self.token}"})
            r.raise_for_status()
            snap = snapshot_from_metrics(r.text)
        except Exception as exc:
            snap = Snapshot(ts=time.time(), reachable=False)
            snap.state, snap.reason = derive_state(snap)
            snap.reason += f": {str(exc)[:120]}"
        self._snap = snap
        metrics.blerbz_state.set(int(snap.state))
        metrics.admissible.set(snap.admissible_mib)
        return snap

    def current(self) -> Snapshot:
        if not workload.enabled():
            return standalone_snapshot()
        s = self._snap
        if not s.ts or time.time() - s.ts > self.stale:
            stale = Snapshot(**{**asdict(s), "state": BlerbzState.IMMINENT})
            stale.reason = "gpusched data stale (fail-safe)" if s.ts else "no data yet (fail-safe)"
            return stale
        return s

    async def run(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self.poll)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self.run())


def _read_token(path: str | None) -> str:
    if not path:
        return ""
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


# ── CAN_RUN ──────────────────────────────────────────────────────────────────

@dataclass
class Job:
    priority: int                 # 0..8 (P0 primary workload … P8 experiments)
    device: str = "cpu"           # cpu | gpu
    mem_mb: int = 0               # new GPU memory (gpu jobs only)
    preemptible: bool = True
    can_wait: bool = True


def can_run(job: Job, snap: Snapshot) -> tuple[str, str]:
    """Deterministic admission. Returns (verdict, reason). No AI involved."""
    st = snap.state
    if job.priority <= 1:
        return Verdict.RUN, "primary-workload production is never blocked"
    if job.device == "gpu":
        if job.mem_mb > snap.admissible_mib:
            if job.can_wait:
                return Verdict.QUEUE, (f"needs {job.mem_mb} MiB GPU; gpusched admits {snap.admissible_mib:.0f} MiB "
                                       "— submit as a gpusched job and wait")
            return Verdict.REJECT, "insufficient GPU memory and the job cannot wait"
        if st >= BlerbzState.HIGH and job.priority >= 4:
            return Verdict.QUEUE, f"primary workload {st.name}: background GPU work deferred"
        return Verdict.QUEUE, "GPU work always goes through a gpusched lease"
    # CPU tiers: bandwidth yield (GB10 CPU and GPU share LPDDR bandwidth)
    if st == BlerbzState.IMMINENT:
        if job.priority >= 5:
            return Verdict.PAUSE_BACKGROUND, f"primary workload IMMINENT ({snap.reason}): batch/eval paused"
        if job.priority == 4:
            return (Verdict.QUEUE if job.can_wait else Verdict.RUN_DEGRADED), "primary workload IMMINENT: async work yields"
        return Verdict.RUN_DEGRADED, "primary workload IMMINENT: interactive runs at reduced concurrency"
    if st == BlerbzState.HIGH and job.priority >= 6:
        return Verdict.QUEUE, "primary workload HIGH: evaluation/experiments deferred"
    return Verdict.RUN, f"primary workload {st.name}"
