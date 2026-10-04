"""Raw platform terms → human language (spec §38, §91): health, states, reasons, activity.

The console never shows CrashLoopBackOff, Pending, IMMINENT or a router reason string on a main
screen; these functions return the human label and the Health bucket, and callers keep the raw
term in `tech`. Reason strings are free text upstream (gateway router.py, batch engine), so parsing
lives here in one place and nothing else string-matches them.

Everything here is pure (no I/O) so the tables are cheap to test and reuse from intent answers.
Owner: SYS.
"""
from __future__ import annotations

import re
from urllib.parse import quote
from collections.abc import Callable, Iterable
from typing import Any

from lif.console.contracts import (ActivityEvent, BlerbzState, Health, JobStatus, ModelRole, RoleCause, RoleKey,
                                   Severity, TechDetail)

# Worst first: the rollup of several healths is the first one present in this order.
SEVERITY_ORDER: tuple[Health, ...] = ("offline", "attention", "degraded", "paused", "busy", "unknown", "healthy")

HEALTH_LABEL: dict[Health, str] = {"healthy": "Healthy", "busy": "Busy", "degraded": "Degraded",
                                   "paused": "Paused", "attention": "Needs attention", "offline": "Offline",
                                   "unknown": "Unknown"}

# gpusched keeps an 8 GiB MemAvailable headroom for its own admissions (config/lif.yaml memory_guard
# comment); below it the box is genuinely tight whatever the BLERBZ state says.
MEM_HEADROOM_MIB = 8192
GPUSCHED_STALE_SEC = 30          # gpusched.stale_after_sec: older data is treated as fail-safe IMMINENT


def health_rollup(items: Iterable[Health]) -> Health:
    """Worst health of a set (empty → unknown)."""
    seen = set(items)
    return next((h for h in SEVERITY_ORDER if h in seen), "unknown")


def tech(**kv: Any) -> list[TechDetail]:
    """Technical Details rows from keyword pairs; None/empty values are skipped (label = key with spaces)."""
    return [TechDetail(label=k.replace("_", " "), value=str(v)[:500]) for k, v in kv.items()
            if v is not None and v != "" and v != [] and v != {}]


def num(v: Any) -> float | None:
    """A finite float from upstream JSON or a Prometheus sample string, else None (never raises)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


# ── Kubernetes (only ever reached through kube-state-metrics; raw term stays in tech) ───────────

K8S: dict[str, tuple[Health, str]] = {
    "crashloopbackoff": ("offline", "Service repeatedly failed to start"),
    "imagepullbackoff": ("offline", "Couldn't download the service image"),
    "errimagepull": ("offline", "Couldn't download the service image"),
    "invalidimagename": ("offline", "The service image name is invalid"),
    "createcontainerconfigerror": ("offline", "The service's configuration is incomplete"),
    "createcontainererror": ("offline", "The service couldn't be created"),
    "runcontainererror": ("offline", "The service couldn't be started"),
    "containercannotrun": ("offline", "The service couldn't be started"),
    "pending": ("busy", "Waiting for resources"),
    "unschedulable": ("attention", "No room on the machine to run this service"),
    "containercreating": ("busy", "Starting up"),
    "podinitializing": ("busy", "Starting up"),
    "oomkilled": ("degraded", "Ran out of memory and restarted"),
    "error": ("attention", "Stopped with an error"),
    "failed": ("offline", "Stopped with an error"),
    "backoff": ("attention", "Restarting after failures"),
    "deadlineexceeded": ("attention", "Took too long and was stopped"),
    "evicted": ("attention", "Stopped by the machine to free resources"),
    "terminating": ("busy", "Shutting down"),
    "notready": ("degraded", "Running but not ready yet"),
    "running": ("healthy", "Running"),
    "ready": ("healthy", "Running"),
    "completed": ("healthy", "Finished"),
    "succeeded": ("healthy", "Finished"),
    "scaledtozero": ("paused", "Stopped"),
    "unknown": ("unknown", "State unknown"),
    "containerstatusunknown": ("unknown", "State unknown"),
}


def k8s_state(term: str) -> tuple[Health, str]:
    """Pod/container state or reason → (health, sentence): CrashLoopBackOff → Service repeatedly failed to start."""
    key = re.sub(r"[^a-z]", "", (term or "").lower())
    return K8S.get(key, ("unknown", "In an unusual state"))


# ── model lifecycle (registry.py: 14 states) ──────────────────────────────────────────────────

MODEL_STATES: dict[str, tuple[str, Health]] = {
    "DISCOVERED": ("Found on Hugging Face", "unknown"),
    "CANDIDATE": ("Shortlisted, not downloaded", "unknown"),
    "DOWNLOADING": ("Downloading and verifying", "busy"),
    "STAGED": ("Downloaded, awaiting benchmark", "unknown"),
    "VALIDATING": ("Starting a test server", "busy"),
    "BENCHMARKING": ("Being evaluated", "busy"),
    "APPROVED": ("Passed evaluation, ready to try", "healthy"),
    "CANARY": ("Trial: serving a share of traffic", "healthy"),
    "PRODUCTION": ("In use", "healthy"),
    "STANDBY": ("Kept as backup", "paused"),
    "DEPRECATED": ("Retired", "unknown"),
    "QUARANTINED": ("Blocked: the download failed verification", "attention"),
    "REJECTED": ("Not good enough", "unknown"),
    "FAILED": ("Failed (download or test)", "attention"),
}


def model_state(state: str) -> tuple[str, Health]:
    """Lifecycle state (PRODUCTION, STANDBY, CANDIDATE…) → (label, health)."""
    return MODEL_STATES.get((state or "").upper(), ("Unknown state", "unknown"))


RECOMMENDATION: dict[str, tuple[str, str]] = {      # comparison report → (code, sentence)
    "CANARY": ("canary", "Better than the current model: worth a trial on a share of traffic."),
    "HOLD": ("hold", "About as good as the current model, not better: kept as an option."),
    "REJECT": ("reject", "Worse than the current model."),
}

FIT: dict[str, str] = {"fits_cpu": "Runs on the CPU tier", "fits_gpu_now": "Fits on the GPU now",
                       "fits_when_gramz_unloaded": "GPU only in a scheduled window",
                       "no_fit": "Too big for this machine"}

_QUANT = re.compile(r"^(q\d.*|iq\d.*|f16|bf16|fp16|fp8|int[48]|gguf|awq|gptq|mlx|k_m|k_s)$", re.I)


def model_name(ref: str | None) -> str:
    """Profile id or HF repo → a friendly name: qwen3-4b-instruct-2507-q4km-cpu → 'Qwen3 4B Instruct 2507 (CPU)'.

    Heuristic on purpose (there is no display-name field upstream); the raw id always stays in tech.
    """
    if not ref:
        return ""
    base = ref.split("@", 1)[0].rsplit("/", 1)[-1]
    parts = [p for p in re.split(r"[-_\s]+", base) if p]
    device = ""
    if parts and parts[-1].lower() in ("cpu", "gpu"):
        device = f" ({parts.pop().upper()})"
    words = []
    for p in parts:
        if _QUANT.match(p):
            continue
        if re.fullmatch(r"\d+(\.\d+)?[bm]", p, re.I):
            words.append(p[:-1] + p[-1].upper())
        elif p.islower() or p.isupper() and len(p) > 4:
            words.append(p[:1].upper() + p[1:].lower())
        else:
            words.append(p)
    return (" ".join(words) or base) + device


# ── roles (aliases) ──────────────────────────────────────────────────────────────────────────

# (role, alias, label, blurb, advanced). Order = display order; advanced roles sit behind "Advanced" (§97).
ROLES: tuple[tuple[RoleKey, str, str, str, bool], ...] = (
    ("auto", "local/auto", "Auto", "Picks the right model for each request.", False),
    ("fast", "local/fast", "Fast", "Best for low-latency requests.", False),
    ("balanced", "local/default", "Balanced", "Good general answers at a moderate speed.", False),
    ("deep", "local/reasoning", "Deep", "Careful reasoning for harder questions.", False),
    ("code", "local/code", "Code", "Writing, reviewing and explaining code.", False),
    ("vision", "local/vision", "Vision", "Understands images.", False),
    ("instant", "local/instant", "Instant", "The smallest, quickest model for tiny tasks.", True),
    ("web", "local/web", "Web", "Reads live web results and answers with sources.", True),
    ("batch", "local/batch", "Batch", "Background batch work.", True),
    ("embedding", "local/embedding", "Embeddings", "Turns text into vectors for search.", True),
    ("rerank", "local/rerank", "Rerank", "Orders search results by relevance.", True),
)
ROLE_BY_KEY = {r[0]: r for r in ROLES}
ROLE_BY_ALIAS = {r[1]: r for r in ROLES}

# Category → alias the controller uses as a candidate's default target (controller/lifecycle.py CATEGORY_ALIAS).
CATEGORY_ALIAS: dict[str, str] = {"fast": "local/fast", "general": "local/default", "coding": "local/code",
                                  "reasoning": "local/reasoning", "embedding": "local/embedding",
                                  "reranking": "local/rerank", "vision": "local/vision", "web": "local/web"}
DISCOVERY_CATEGORIES = ("fast", "general", "coding", "reasoning", "embedding", "reranking", "vision")


def role_label(alias: str) -> str:
    r = ROLE_BY_ALIAS.get(alias)
    return r[2] if r else alias


_FLOOR = re.compile(r"expects >= ?([\d.]+) ?B; largest resident model is ([\d.]+) ?B", re.I)
_CANARY = re.compile(r"canary \((\d+(?:\.\d+)?)% of", re.I)
_YIELD = ("reserved for primary-workload", "reserved for the primary workload", "latency budget during")


def role_cause(reason: str | None, *, fallback: bool, degraded: bool, shed: bool = False,
               canary: bool = False) -> tuple[RoleCause | None, str | None]:
    """Gateway alias status (+ memory-guard shedding) → structured cause and its human label.

    Order matters: a shed primary shows up upstream as a raw connection error, so the guard explains it
    first; a fallback explains more than a size floor (degraded is often a side effect of falling back).
    """
    r = (reason or "").strip()
    low = r.lower()
    m = _CANARY.search(r)
    if canary or m:
        pct = f"{m.group(1)}% of" if m else "a share of"
        return "canary", f"Trialling a new model on {pct} requests"
    if shed:
        return "shed_by_memory_guard", "Main model paused to free memory for BLERBZ"
    if "promoted to local/web yet" in low:
        return "not_deployed", "No dedicated model yet: the Balanced model answers with web results"
    if low.startswith("no local model is deployed") or "no local model is deployed" in low:
        return "not_deployed", "No model is installed for this role"
    # The router appends "(GPU reserved for the primary workload)" to every size-floor message, so a floor
    # is matched before the yield phrases and is never reported as BLERBZ's doing.
    f = _FLOOR.search(r)
    if f and not fallback:
        return "below_quality_floor", (f"Running on a smaller model than this role expects "
                                       f"({_trim(f.group(2))}B instead of {_trim(f.group(1))}B or more)")
    if any(k in low for k in _YIELD):
        if "latency budget" in low:
            return "yielded_to_primary", "Switched to a faster model while BLERBZ is running"
        return "yielded_to_primary", "Main model paused while BLERBZ is running; using a backup"
    if low.startswith("all models for"):
        return "primary_unavailable", "All models for this role are offline"
    if low.startswith("upstream_error on"):
        return "primary_unavailable", "Main model failed mid-request; a backup answered"
    if low.startswith("primary unavailable"):
        return "primary_unavailable", "Main model offline; using a backup"
    if degraded:
        return "below_quality_floor", "Running on a smaller model than this role expects"
    if fallback:
        return "unknown", "Served by a backup model"
    if r:
        return "unknown", "Not available right now"
    return None, None


def _trim(x: str) -> str:
    return x[:-2] if x.endswith(".0") else x


# ── BLERBZ (the primary workload) from the gpusched snapshot (gpu/state.py) ────────────────────

def blerbz(snapshot: dict[str, Any]) -> tuple[BlerbzState, str, str]:
    """Controller /v1/gpu snapshot → (state, label, one-sentence reason).

    IMMINENT has three very different causes upstream (production running, residents reloading,
    gpusched blind); they are told apart by fields, never by the reason text alone.
    """
    if not isinstance(snapshot, dict) or not snapshot:
        return "unknown", "Unknown", "GPU scheduler status hasn't been read yet."
    state = str(snapshot.get("state") or "").upper()
    reason = str(snapshot.get("reason") or "")
    age = num(snapshot.get("age_sec"))
    if not snapshot.get("reachable", True) or "unreachable" in reason or (age is not None and age > GPUSCHED_STALE_SEC) \
            or "stale" in reason or "no data yet" in reason:
        return ("unknown", "Unknown",
                "Labzilla can't see the GPU scheduler, so it holds background work as if BLERBZ were running.")
    if snapshot.get("production_live"):
        return "busy", "Generating", "GPU reserved for BLERBZ video generation."
    residents = snapshot.get("residents_loaded") or {}
    if isinstance(residents, dict) and any(v is False for v in residents.values()) or "reloading" in reason:
        return "busy", "Reloading", "BLERBZ is reloading its models; local AI runs slower until it finishes."
    if state == "IMMINENT":
        return "reserved", "Reserved", "GPU held for BLERBZ; local AI runs one request at a time."
    if state == "HIGH":
        return "imminent", "Likely soon", "BLERBZ is likely to start within the hour; evaluations are deferred."
    if state == "MODERATE":
        if "no forecast" in reason:
            return "idle", "Idle", "BLERBZ is idle (no forecast available for the next hour)."
        return "idle", "Idle", "BLERBZ is idle but may start soon."
    if state == "LOW":
        return "idle", "Idle", "BLERBZ is idle; production is unlikely in the next hour."
    return "unknown", "Unknown", "GPU scheduler reported an unrecognised state."


# ── jobs (batch engine states + controller background task strings) ───────────────────────────

_JOB_TERMINAL: dict[str, tuple[JobStatus, str]] = {
    "completed": ("completed", "Finished"), "done": ("completed", "Finished"),
    "succeeded": ("completed", "Finished"), "failed": ("failed", "Failed"),
    "cancelled": ("cancelled", "Cancelled"), "canceled": ("cancelled", "Cancelled"),
    "expired": ("failed", "Expired: deadline passed"), "interrupted": ("failed", "Interrupted"),
}


def job_state(status: str, reason: str | None) -> tuple[JobStatus, str, bool]:
    """Batch status + free-text reason → (status, status label, resumes_automatically).

    Also accepts controller task strings ('running' | 'done' | 'failed: <msg>').
    """
    s = (status or "").strip().lower()
    r = (reason or "").strip().lower()
    if s.startswith("failed:"):
        return "failed", "Failed", False
    if s in _JOB_TERMINAL:
        return (*_JOB_TERMINAL[s], False)
    if s == "paused":
        return "paused", "Paused", False
    if "batch paused by operator" in r or "maintenance" in r:
        return "paused", "Paused by an operator", False
    if "fail-safe" in r or "unreachable" in r or "stale" in r:
        return "waiting", "Waiting: GPU status unknown", True
    if "imminent" in r or "primary workload" in r and "high" not in r:
        return "waiting", "Waiting for BLERBZ", True
    if "high" in r and "defer" in r:
        return "waiting", "Deferred until BLERBZ is quiet", True
    if r.startswith("waiting"):
        return "waiting", "Waiting for capacity", True
    if s == "running":
        return "running", "Running", False
    if s in ("queued", "pending"):
        return "queued", "Queued", False
    return "queued", "Queued", False


_HTTP_CODE = re.compile(r"\s*\(?\bHTTP[ /]?\d{3}\b\)?", re.I)


def run_error(error: Any, default: str = "The run reported an error.", limit: int = 200) -> str:
    """A run's upstream error as a sentence for the main UI: protocol status codes are dropped (§81; the raw
    text stays in Technical details)."""
    text = _HTTP_CODE.sub("", str(error or "")).strip(" :;,-")
    return (text or default)[:limit]


def job_reason(reason: str | None) -> str | None:
    """Human sentence for a batch wait reason (§31), or None when there is nothing to explain."""
    r = (reason or "").lower()
    if not r:
        return None
    if "batch paused by operator" in r or "maintenance" in r:
        return "All batch work is paused by an operator."
    if "fail-safe" in r or "unreachable" in r or "stale" in r:
        return "GPU scheduler status is unknown, so work is held to be safe."
    if "imminent" in r:
        return "GPU reserved for BLERBZ video generation."
    if "high" in r and "defer" in r:
        return "BLERBZ is likely to start soon; low-priority work runs later."
    if "paused by owner" in r:
        return "Paused by its owner."
    if "deadline" in r:
        return "The deadline passed before every item ran."
    if "cancelled" in r:
        return "Cancelled by its owner."
    return None


# ── activity (controller registry activity table) ──────────────────────────────────────────────

ACTORS: dict[str, str] = {"operator": "Admin", "operator-force": "Admin (override)", "controller": "Automatic",
                          "system": "System", "gpu-resource-manager": "Memory protection",
                          "scheduler": "Scheduled", "retention": "Automatic cleanup", "bootstrap": "Initial setup",
                          "console": "Console"}

_Row = dict[str, Any]
_Spec = tuple[str, Severity, Callable[[_Row], str], Callable[[_Row], str | None], Callable[[_Row], str | None]]


def _d(row: _Row) -> dict[str, Any]:
    d = row.get("detail")
    return d if isinstance(d, dict) else {}


def _m(row: _Row) -> str:
    return model_name(str(row.get("subject") or "")) or "a model"


def _href_model(row: _Row) -> str | None:
    s = str(row.get("subject") or "")
    return f"/models/deployments/{quote(s, safe='')}" if s else None


def _href_candidate(row: _Row) -> str | None:
    s = str(row.get("subject") or "")
    return f"/models/candidates/{quote(s, safe='')}" if s else None


def _href_alias(row: _Row) -> str | None:
    r = ROLE_BY_ALIAS.get(str(row.get("subject") or ""))
    return f"/models/roles/{r[0]}" if r else "/models"


def _bench_detail(row: _Row) -> str | None:
    d = _d(row)
    bits = []
    if num(d.get("quality")) is not None:
        bits.append(f"Quality {round(num(d['quality']) * 100)}%")
    if num(d.get("ttft_ms_p50")) is not None:
        bits.append(f"first token {num(d['ttft_ms_p50']) / 1000:.1f} s")
    if num(d.get("decode_tps_p50")) is not None:
        bits.append(f"{num(d['decode_tps_p50']):.0f} tokens/s")
    return " · ".join(bits) or None


def _state_title(row: _Row) -> str:
    d = _d(row)
    to = model_state(str(d.get("to") or ""))[0]
    return f"{_m(row)}: {to.lower()}" if d.get("to") else f"{_m(row)} changed state"


def _setting_title(row: _Row) -> str:
    key = str(row.get("subject") or "a setting")
    v = _d(row).get("value")
    label = SETTING_LABELS.get(key, key.replace("_", " "))
    if isinstance(v, bool):
        return f"{label} turned {'on' if v else 'off'}"
    return f"{label} set to {v}"


def _shed_title(verb: str) -> Callable[[_Row], str]:
    def f(row: _Row) -> str:
        svc = service_name(str(row.get("subject") or "")) if row.get("subject") else "a model service"
        return f"Resumed {svc}" if verb == "Resumed" else f"{verb} {svc} to free memory"
    return f


def _alias_title(row: _Row) -> str:
    chain = _d(row).get("chain")
    first = chain[0] if isinstance(chain, list) and chain else ""
    return f"{role_label(str(row.get('subject')))} now served by {model_name(str(first)) or 'a new model list'}"


def _shed_detail(row: _Row) -> str | None:
    mib = num(_d(row).get("mem_available_mib"))
    return f"{mib / 1024:.1f} GB of memory was free at the time." if mib is not None else None


def _discovery_finished(row: _Row) -> str:
    sl = _d(row).get("shortlisted")
    n = len(sl) if isinstance(sl, list) else num(sl)
    if n is None:
        return "Model check finished"
    return f"Model check finished: {int(n)} candidate{'s' if n != 1 else ''} worth testing"


def _comparison(row: _Row) -> str:
    rec = str(_d(row).get("recommendation") or "").upper()
    return {"CANARY": f"{_m(row)} beats the current model", "HOLD": f"{_m(row)} is about as good as the current model",
            "REJECT": f"{_m(row)} is worse than the current model"}.get(rec, f"{_m(row)} compared with the current model")


def _none(_: _Row) -> None:
    return None


def _decision_title(row: _Row) -> str:
    kind = str(row.get("kind") or "")
    subj = str(row.get("subject") or "")
    d = _d(row)
    if kind == "decision_transition":
        return f"Decision {subj} moved to {DECISION_STAGES.get(str(d.get('stage')), d.get('stage') or 'a new stage')}"
    if kind == "decision_rollback":
        return f"Decision {subj} rolled back"
    if kind == "decision_human":
        return "A decision review was answered"
    if kind == "decision_inventory":
        return "Decision inventory updated"
    return "Decision settings changed"


ACTIVITY: dict[str, _Spec] = {
    # kind: (category, severity, title, detail, href)
    "model_registered": ("model", "info", lambda r: f"New model found: {_m(r)}", _none, _href_model),
    "state_changed": ("model", "info", _state_title, lambda r: _d(r).get("reason") or None, _href_model),
    "model_pinned": ("model", "info", lambda r: f"{_m(r)} kept (protected from cleanup)", _none, _href_model),
    "model_unpinned": ("model", "info", lambda r: f"{_m(r)} no longer kept", _none, _href_model),
    "model_blocked": ("model", "warning", lambda r: f"{_m(r)} blocked", _none, _href_model),
    "model_unblocked": ("model", "info", lambda r: f"{_m(r)} unblocked", _none, _href_model),
    "model_deleted": ("model", "info", lambda r: f"{_m(r)} removed", _none, lambda r: "/models"),
    "alias_changed": ("model", "success", _alias_title, lambda r: _d(r).get("note") or None, _href_alias),
    "alias_rollback": ("model", "warning", lambda r: f"{role_label(str(r.get('subject')))} rolled back"
                       + (f" to version {_d(r)['to_version']}" if _d(r).get("to_version") else ""), _none, _href_alias),
    "benchmark_completed": ("model", "success", lambda r: f"Benchmark finished for {_m(r)}", _bench_detail, _href_model),
    "comparison_complete": ("model", "info", _comparison, _none, _href_candidate),
    "load_test_passed": ("model", "success", lambda r: f"{_m(r)} starts correctly", _none, _href_model),
    # Automatic promotion (controller/autopromote.py): every step names the measured reason.
    "auto_canary_started": ("model", "info", lambda r: f"Trying {_m(r)} on part of the traffic", _none, _href_model),
    "auto_promoted": ("model", "success", lambda r: f"{_m(r)} promoted automatically",
                      lambda r: "; ".join(_d(r).get("reasons") or []) or None, _href_model),
    "auto_canary_ended": ("model", "warning", lambda r: f"{_m(r)} kept out of production",
                          lambda r: "; ".join(_d(r).get("reasons") or []) or None, _href_model),
    "download_started": ("job", "info", lambda r: f"Downloading {_m(r)}", _none, _href_model),
    "download_complete": ("job", "success", lambda r: f"Download complete: {_m(r)}", _none, _href_model),
    "model_loaded": ("model", "info", lambda r: f"{_m(r)} started", _none, _href_model),
    "model_unloaded": ("model", "info", lambda r: f"{_m(r)} stopped", lambda r: _d(r).get("reason") or None, _href_model),
    "retention_gc": ("system", "info", lambda r: "Cleaned up old model files", _none, lambda r: "/system/storage"),
    "discovery_started": ("agent", "info", lambda r: "Checking for better models", _none, lambda r: "/models/discovery"),
    "discovery_finished": ("agent", "success", _discovery_finished, _none, lambda r: "/models/discovery"),
    "discovery_failed": ("agent", "error", lambda r: "Model check failed", lambda r: str(_d(r).get("error") or "")[:200] or None,
                         lambda r: "/models/discovery"),
    "memory_guard_shed": ("system", "warning", _shed_title("Paused"), _shed_detail, lambda r: "/system/compute"),
    "memory_guard_reshed": ("system", "warning", _shed_title("Paused"), _shed_detail, lambda r: "/system/compute"),
    "memory_guard_restore": ("system", "success", _shed_title("Resumed"), _shed_detail, lambda r: "/system/compute"),
    "blerbz_state": ("system", "info", lambda r: f"BLERBZ status: {blerbz({'state': r.get('subject')})[1].lower()}",
                     _none, lambda r: "/system/compute"),
    "blerbz_takeover": ("system", "info", lambda r: "BLERBZ started: local AI stepped back", _none,
                        lambda r: "/system/compute"),
    "blerbz_release": ("system", "success", lambda r: "BLERBZ finished: local AI back to normal", _none,
                       lambda r: "/system/compute"),
    "controller_started": ("system", "info", lambda r: "Model control service restarted", _none, lambda r: "/system/services"),
    "setting_changed": ("system", "info", _setting_title, _none, lambda r: "/system/settings"),
}
_DECISION_SPEC: _Spec = ("decision", "info", _decision_title, lambda r: _d(r).get("reason") or None, lambda r: "/agents")

# Re-discovery flaps CANDIDATE → DISCOVERED → CANDIDATE on the same id (discovery.py upsert); it is noise.
_NOISE_STATES = {"DISCOVERED", "CANDIDATE"}


def activity(row: dict[str, Any]) -> ActivityEvent | None:
    """Controller activity row {seq, ts, kind, subject, actor, detail} → ActivityEvent (None = noise, drop)."""
    if not isinstance(row, dict):
        return None
    kind = str(row.get("kind") or "")
    d = _d(row)
    if kind == "state_changed" and {str(d.get("frm")), str(d.get("to"))} <= _NOISE_STATES:
        return None
    spec = ACTIVITY.get(kind) or (_DECISION_SPEC if kind.startswith("decision_") else None)
    actor = str(row.get("actor") or "")
    raw = tech(kind=kind, subject=row.get("subject"), actor=actor, by=ACTORS.get(actor), seq=row.get("seq"))
    if spec is None:            # unknown kinds still show, plainly, rather than vanish
        return ActivityEvent(id=f"act:{row.get('seq')}", ts=num(row.get("ts")) or 0.0, category="system",
                             title=kind.replace("_", " ").capitalize() or "Event", tech=raw)
    category, severity, title, detail, href = spec
    if kind == "state_changed" and str(d.get("to")) in ("FAILED", "QUARANTINED"):
        severity = "error"
    elif kind == "state_changed" and str(d.get("to")) in ("PRODUCTION", "APPROVED"):
        severity = "success"
    elif kind == "comparison_complete" and str(d.get("recommendation")).upper() == "CANARY":
        severity = "success"
    try:
        t, det, h = title(row), detail(row), href(row)
    except (TypeError, ValueError, KeyError, IndexError, AttributeError):
        t, det, h = kind.replace("_", " ").capitalize(), None, None
    return ActivityEvent(id=f"act:{row.get('seq')}", ts=num(row.get("ts")) or 0.0, category=category,  # type: ignore[arg-type]
                         severity=severity, title=t, detail=str(det)[:300] if det else None, href=h, tech=raw)


# ── decisions ─────────────────────────────────────────────────────────────────────────────────

DECISION_ROUTES: dict[str, str] = {
    "auto": "Decided automatically (high confidence)",
    "validated": "Decided, confirmed by a check",
    "escalated": "Decided by a stronger model",
    "baseline": "Decided by the agent's existing logic",
    "human": "Waiting for a person to review (safe default used)",
    "safe_default": "Couldn't decide: used the safe choice",
    "advisory": "Advice only: needs a person",
}
DECISION_STAGES: dict[str, str] = {"designed": "draft", "tested": "tested", "shadow": "trial (observing only)",
                                   "calibrated": "tuned", "low_risk_automation": "live for 10%",
                                   "expanded_automation": "live for 50%", "production": "live"}
REVIEW_REASONS: dict[str, str] = {
    "below_threshold": "The model wasn't confident enough", "below_low": "The model wasn't confident enough",
    "flat_distribution": "Options were too close to call", "exit_answer": "The model said it couldn't tell",
    "jev_unavailable": "The decision service was unavailable", "policy_review": "Policy requires a person",
    "shadow_disagreement": "A new version disagreed with the agent", "shadow_sample": "Routine spot check",
}


def decision_route(route: str) -> str:
    """Cascade route (auto, validated, escalated, human…) → 'Decided automatically (high confidence)'."""
    return DECISION_ROUTES.get((route or "").lower(), "Decided")


# ── services, settings, alerts ───────────────────────────────────────────────────────────────

SERVICE_NAMES: dict[str, str] = {
    "gateway": "AI gateway", "controller": "Model control service", "batch": "Batch service",
    "decision": "Decision service", "knowledge": "Knowledge service", "prometheus": "Metrics history",
    "gpusched": "GPU scheduler",
    # ai-serving deployments owned by the memory guard (not BLERBZ's own GPU embedder, which is a gpusched resident)
    "tier0": "Main CPU model server", "tier0-small": "Small backup model server",
    "embedding": "LIF embedding service",
}


def service_name(key: str) -> str:
    return SERVICE_NAMES.get(key, model_name(key) if key else "a service")


SETTING_LABELS: dict[str, str] = {
    "maintenance": "Maintenance mode", "batch_paused": "Batch pause", "automatic_discovery": "Automatic model checks",
    "automatic_download": "Automatic downloads", "automatic_promotion": "Automatic promotion",
    "discovery_disabled": "Model checks disabled", "jev_disabled": "Jev decisions disabled",
    "reserve_gpu_mib": "Extra memory reserve",
}

ALERTS: dict[str, str] = {
    "LIFUsefulAIUnavailable": "Local AI can't answer chat requests",
    "LIFGatewayDown": "The AI gateway is down",
    "LIFDefaultDegraded": "The default model is running degraded",
    "LIFJevCircuitOpen": "Jev decisions are paused after errors (rules only)",
    "LIFHostHeadroomLow": "Memory is below the safety margin",
    "LIFEscalationProviderCircuitOpen": "A decision escalation provider is paused after errors",
    "LIFDecisionSafeDefaultsHigh": "Many decisions are falling back to the safe default",
    "LIFHumanReviewBacklog": "Decision reviews are piling up",
    "LIFDecisionOutcomeErrors": "Decision outcomes are failing to record",
    "LIFSQLiteBackupStale": "The nightly database backup is overdue",
    "LIFSQLiteBackupFailed": "The nightly database backup failed",
    "NodeMemoryMajorPagesFaults": "The machine is swapping memory heavily",
    "HomelabGpuReserveBreached": "The GPU memory reserve was breached",
}
IGNORED_ALERTS = {"Watchdog", "InfoInhibitor"}


def alert_severity(label: str | None) -> Severity:
    s = (label or "").lower()
    return "error" if s in ("critical", "error", "page") else "info" if s in ("info", "none") else "warning"


def alert_summary(name: str, annotations_summary: str | None = None) -> str:
    return ALERTS.get(name) or (annotations_summary or "") or re.sub(r"(?<!^)(?=[A-Z])", " ", name).capitalize()


# ── health rollups (the "Is anything wrong?" answer, §2) ─────────────────────────────────────

Reason = tuple[Health, str]


def local_ai(gateway_health: dict[str, Any] | None, roles: list[ModelRole], blerbz_state: BlerbzState) -> tuple[Health, str]:
    """Home's 'Local AI Ready' line from gateway /v1/health and the Fast/Balanced roles."""
    if not isinstance(gateway_health, dict):
        return "offline", "Local AI unreachable"
    if not gateway_health.get("useful_local_ai", gateway_health.get("status") == "ok"):
        return "offline", "Local AI unavailable"
    by = {r.role: r for r in roles}
    bal = by.get("balanced")
    if bal and bal.fallback_active and bal.cause != "canary":
        return "degraded", "Local AI on fallback model"
    if blerbz_state == "busy":
        return "busy", "Local AI Ready · slower while BLERBZ runs"
    return "healthy", "Local AI Ready"


def system_health(*, gateway_health: dict[str, Any] | None, controller_ok: bool, gpu: dict[str, Any] | None,
                  mem_available_mib: float | None, shed: list[str], roles: list[ModelRole],
                  alerts: list[dict[str, Any]], maintenance: bool, batch_paused: bool,
                  decision: dict[str, Any] | None, not_connected: list[str] | None = None) -> tuple[Health, str, list[Reason]]:
    """Overall health from several sources, never BLERBZ alone (it can read 'idle' while memory is tight).

    Returns (health, headline, reasons) — reasons are (health, sentence) pairs, worst first, so the
    headline is always the most important one and the rest can be listed under it.
    """
    reasons: list[Reason] = []
    if not isinstance(gateway_health, dict):
        reasons.append(("offline", "Local AI is unreachable"))
    elif not gateway_health.get("useful_local_ai", gateway_health.get("status") == "ok"):
        reasons.append(("offline", "Local AI is unavailable"))
    if maintenance:
        reasons.append(("paused", "Maintenance mode is on"))
    by = {r.role: r for r in roles}
    for key, what in (("balanced", "Running on fallback model"), ("fast", "Fast model on fallback")):
        r = by.get(key)
        if r and r.fallback_active and r.cause != "canary":
            reasons.append(("degraded", what))
    if mem_available_mib is not None and mem_available_mib < MEM_HEADROOM_MIB:
        reasons.append(("degraded", "Memory is below the safety margin"))
    if shed:
        reasons.append(("degraded", "Some optional AI services are paused to free memory"))
    for sentence in not_connected or []:      # a missing/rejected console key is a setup step, not an outage
        reasons.append(("attention", sentence))
    if not controller_ok:
        reasons.append(("attention", "Model control service isn't answering"))
    if isinstance(gpu, dict) and gpu and blerbz(gpu)[0] == "unknown":
        reasons.append(("attention", "GPU scheduler isn't reporting"))
    if isinstance(decision, dict) and decision.get("jev_breaker_open"):
        reasons.append(("degraded", "Decisions are using rules only (Jev paused after errors)"))
    for a in alerts:
        name = str((a.get("metric") or {}).get("alertname") or "")
        if not name or name in IGNORED_ALERTS:
            continue
        sev = alert_severity((a.get("metric") or {}).get("severity"))
        if sev == "info":
            continue
        reasons.append(("attention" if sev == "error" else "degraded", alert_summary(name)))
    if isinstance(gpu, dict) and blerbz(gpu)[0] == "busy":
        reasons.append(("busy", "BLERBZ is using the GPU"))
    if batch_paused and not maintenance:
        reasons.append(("healthy", "Batch jobs are paused"))
    # de-duplicate sentences (an alert often restates a direct signal) keeping the worst first
    seen: set[str] = set()
    ordered = [r for r in sorted(reasons, key=lambda x: SEVERITY_ORDER.index(x[0]))
               if not (r[1] in seen or seen.add(r[1]))]
    health = health_rollup(h for h, _ in ordered) if ordered else "healthy"
    if health == "unknown":
        health = "healthy"
    worst = [s for h, s in ordered if h == health]
    if health == "healthy":
        headline = "Labzilla is healthy" + (" · batch jobs paused" if batch_paused and not maintenance else "")
    else:
        headline = worst[0] if worst else HEALTH_LABEL[health]
    return health, headline, ordered


def jev_improves(v: Any) -> bool | None:
    """Jev's candidate-vs-incumbent verdict. The controller stores the decision string ("yes"/"no", a
    noul decision), never a bool, so truthiness would read "no" as an endorsement. Unknown → None."""
    if isinstance(v, bool):
        return v
    t = str(v or "").strip().lower()
    return True if t in ("yes", "true") else False if t in ("no", "false") else None
