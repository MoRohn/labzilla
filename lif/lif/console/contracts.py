"""Console domain contracts: the only shapes the browser ever sees (spec §90).

Single source of truth for the console API. `python -m lif.console.gen_ts` mirrors every model and
Literal alias here into `apps/console/src/api/contracts.gen.ts`, so the PWA and the BFF cannot drift.

Rules that keep the UI honest and simple:
- No raw Kubernetes or upstream objects. Raw states, pod names, revisions and reason strings go in
  `tech: list[TechDetail]`, which feeds the Technical Details drawer (§42, §79) and nothing else.
- Every field has a default, so a route can build a partial object when an upstream is down and
  say so (`stale`, `available=False`, `reason`) instead of failing the whole response (§60).
- `X | None = None` means "unknown / not measured"; the UI shows a dash or a reason, never a guess.
- Timestamps are Unix epoch seconds (float, UTC). Knowledge dates stay ISO date strings.
- Every API error body is `ErrorBody` ({"error": HumanError}); bare HTTP codes never reach the UI (§81).

Frozen after the ARCH phase: add a field by asking for it (contract_requests), not by editing here.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    """Base: `model_*` field names are domain words here, not pydantic internals."""
    model_config = ConfigDict(protected_namespaces=(), extra="ignore")


# ── shared vocabularies ───────────────────────────────────────────────────────────────────────

Health = Literal["healthy", "busy", "degraded", "paused", "attention", "offline", "unknown"]
Severity = Literal["info", "success", "warning", "error"]
ConfirmKind = Literal["none", "simple", "typed"]
DecidedBy = Literal["rules", "jev", "local_model", "human", "code"]
Perm = Literal["read", "ask", "jobs.control", "approvals.answer", "models.discover", "models.operate",
               "models.release", "system.safe", "system.settings", "devices.manage"]
UserRole = Literal["admin", "device"]
# What the user asks for (request side) vs what actually happened (receipt side, §64).
PrivacyChoice = Literal["local_only", "allow_jev"]      # local_only = CONFIDENTIAL; allow_jev = PUBLIC
PrivacyUsed = Literal["local_only", "local_jev", "external"]
AskMode = Literal["auto", "fast", "balanced", "deep", "code", "vision"]    # §10 logical capabilities
RoleKey = Literal["auto", "fast", "balanced", "deep", "code", "vision", "instant", "batch", "embedding", "rerank", "web"]


class TechDetail(Contract):
    """One raw fact for the Technical Details drawer (k8s resource, pod, revision, raw reason, request id)."""
    label: str = ""
    value: str = ""


class Fact(Contract):
    """A human label/value pair shown inline (command answers, decision 'why' rows)."""
    label: str = ""
    value: str = ""


# ── compute / resources (§6, §36, §37) ────────────────────────────────────────────────────────

BlerbzState = Literal["idle", "busy", "imminent", "reserved", "unknown"]
Temperature = Literal["normal", "warm", "hot", "unknown"]
WorkKey = Literal["blerbz", "ai_serving", "other", "available"]


class ResourceState(Contract):
    """What the machine is doing. Memory is unified (CPU+GPU); None = not measurable right now."""
    # `blerbz` is the historical wire name of the optional primary-workload state.
    primary_enabled: bool = False           # false: no external workload; the console hides that row
    primary_name: str = ""                  # console label when primary_enabled (operator-configured)
    blerbz: BlerbzState = "unknown"
    blerbz_label: str = ""                  # "Idle", "Running", "Reserved"
    blerbz_reason: str = ""                 # one human sentence; raw scheduler reason lives in tech
    gpu_util_pct: float | None = None
    mem_total_gb: float | None = None
    mem_used_gb: float | None = None
    mem_available_gb: float | None = None
    blerbz_mem_gb: float | None = None
    ai_serving_mem_gb: float | None = None
    other_mem_gb: float | None = None
    temperature: Temperature = "unknown"
    temperature_note: str = ""              # e.g. "No GPU temperature sensor is exported yet"
    stale: bool = False
    updated_at: float = 0.0
    tech: list[TechDetail] = []


class WorkShare(Contract):
    """One segment of 'Current work' (§36): primary workload 18%, AI Serving 24%, Available 58%."""
    key: WorkKey = "other"
    label: str = ""
    gb: float | None = None
    pct: float | None = None


class ComputeView(Contract):
    """GET /api/system/compute."""
    device: str = "DGX Spark"
    resource: ResourceState = ResourceState()
    work: list[WorkShare] = []
    note: str = ""                          # e.g. "Memory split needs Prometheus; showing totals only"
    tech: list[TechDetail] = []


class TimelineBucket(Contract):
    """One hour of the workload timeline (§37)."""
    start: float = 0.0
    end: float = 0.0
    label: str = ""                         # "inference", "idle", "GPU", "inference + batch"
    blerbz_pct: float | None = None
    ai_pct: float | None = None
    batch_items: int | None = None


class TimelineResponse(Contract):
    """GET /api/system/timeline. available=False + reason when Prometheus can't answer."""
    hours: int = 12
    buckets: list[TimelineBucket] = []
    available: bool = True
    reason: str | None = None
    primary_enabled: bool = False
    primary_name: str = ""


# ── models (§10, §26–§29) ────────────────────────────────────────────────────────────────────

RoleCause = Literal["canary", "primary_unavailable", "below_quality_floor", "not_deployed",
                    "shed_by_memory_guard", "yielded_to_primary", "unknown"]
MemoryBasis = Literal["configured", "estimated", "measured"]
SpeedBasis = Literal["measured", "estimated", "live"]
ModelAction = Literal["load", "unload", "benchmark", "download", "test", "promote", "canary", "delete",
                      "block", "unblock", "pin", "unpin"]
PreviewAction = Literal["promote", "canary", "rollback", "unload", "delete", "load"]


class CanaryInfo(Contract):
    model: str = ""
    percent: float = 0.0


class ModelRole(Contract):
    """A logical capability (local/fast, local/default…) and who serves it right now."""
    role: RoleKey = "auto"
    alias: str = ""                         # "local/fast"
    label: str = ""                         # "Fast"
    blurb: str = ""                         # "Best for low-latency requests." (§96)
    state: Health = "unknown"
    state_label: str = ""                   # "Ready" | "Standby" | "On demand" | "Offline" | "Degraded"
    model_name: str = ""                    # friendly name of what serves it now
    physical_model: str | None = None       # repo@revision, for "View physical model" (§10)
    served_by: str | None = None            # profile id
    fallback_active: bool = False
    degraded: bool = False
    cause: RoleCause | None = None
    cause_label: str | None = None          # "Default model paused to free memory for the primary workload"
    chain: list[str] = []                   # friendly names, primary first
    canary: CanaryInfo | None = None
    tech: list[TechDetail] = []


class BenchmarkSummary(Contract):
    ts: float = 0.0
    suite: str = ""
    quality: float | None = None            # 0..1 pass rate
    ttft_ms_p50: float | None = None
    decode_tps_p50: float | None = None
    latency_ms_p50: float | None = None
    errors: int = 0
    items: int = 0


class ModelDeployment(Contract):
    """A physical model the platform knows about (production/standby on /api/models; candidates elsewhere)."""
    id: str = ""                            # registry profile id
    name: str = ""                          # friendly
    model_id: str = ""                      # Hugging Face repo
    revision: str = ""
    source: str = "Hugging Face"
    runtime: str = ""                       # "llama.cpp CPU"
    device: str = ""
    precision: str = ""
    params_b: float | None = None
    context: int | None = None
    state: str = ""                         # raw lifecycle state (PRODUCTION, STANDBY, CANDIDATE…)
    state_label: str = ""                   # "In use", "Kept as backup", "Shortlisted, not downloaded"
    health: Health = "unknown"
    roles: list[str] = []                   # aliases this model serves or is primary for
    memory_gb: float | None = None
    memory_basis: MemoryBasis = "configured"
    speed_tps: float | None = None
    ttft_ms: float | None = None
    speed_basis: SpeedBasis | None = None
    quality: float | None = None
    last_evaluated: float | None = None     # None = never benchmarked
    usage_24h: int | None = None            # requests served in the last 24 h
    pinned: bool = False
    blocked: bool = False
    actions: list[ModelAction] = []         # actions valid in this state (permission still checked server-side)
    tech: list[TechDetail] = []


class ActivityEvent(Contract):
    """One human-readable thing that happened (§6 Recent Activity, §80 relevant events)."""
    id: str = ""
    ts: float = 0.0
    category: Literal["model", "agent", "job", "system", "decision", "security", "knowledge"] = "system"
    severity: Severity = "info"
    title: str = ""
    detail: str | None = None
    href: str | None = None
    tech: list[TechDetail] = []


class DeploymentDetail(Contract):
    """GET /api/models/deployments/{id}."""
    deployment: ModelDeployment = ModelDeployment()
    benchmarks: list[BenchmarkSummary] = []
    activity: list[ActivityEvent] = []


class ComparisonRow(Contract):
    metric: str = ""                        # "Quality", "Time to first token", "Throughput", "Memory", "Stability"
    current: float | None = None
    candidate: float | None = None
    unit: str = ""
    better: Literal["higher", "lower"] = "higher"
    verdict: Literal["better", "worse", "same", "unknown"] = "unknown"
    note: str | None = None                 # "estimated", "not benchmarked yet"


class CandidateComparison(Contract):
    """Side-by-side current vs candidate for one role (§29)."""
    candidate: ModelDeployment = ModelDeployment()
    incumbent: ModelDeployment | None = None
    role: str = ""                          # alias the candidate would serve
    rows: list[ComparisonRow] = []
    recommendation: str = ""                # human sentence
    recommendation_code: Literal["canary", "hold", "reject", "benchmark_first", "unknown"] = "unknown"
    advisory: str | None = None             # Jev advisory, labelled as advice
    tech: list[TechDetail] = []


class CandidateItem(Contract):
    deployment: ModelDeployment = ModelDeployment()
    comparison_hint: str = ""               # "Faster than Fast's current model; not benchmarked yet"
    role: RoleKey | None = None


class DiscoveryStage(Contract):
    key: Literal["discovering", "filtering", "evaluating", "benchmarking"] = "discovering"
    label: str = ""                         # "Discovering (2,000 models found)"
    count: int | None = None
    done: bool = False


class DiscoveryRun(Contract):
    """A 'Check for better models' run (§28). Stage counts appear only when the run finishes."""
    id: str = ""
    started_at: float = 0.0
    finished_at: float | None = None
    status: Literal["running", "succeeded", "failed", "interrupted"] = "running"
    stages: list[DiscoveryStage] = []
    summary: str = ""                       # "2 meaningful upgrades found"
    upgrades: int = 0
    categories: list[str] = []
    tech: list[TechDetail] = []


class DiscoveryState(Contract):
    """GET /api/models/discovery."""
    current: DiscoveryRun | None = None
    recent: list[DiscoveryRun] = []
    enabled: bool = True
    disabled_reason: str | None = None      # why checks can't start (only meaningful when enabled is false)
    note: str | None = None                 # non-blocking remark, e.g. the last request ended without a run


class ModelsOverview(Contract):
    """GET /api/models: the portfolio (roles first), not every downloaded file (§26)."""
    roles: list[ModelRole] = []
    deployments: list[ModelDeployment] = []
    candidates_count: int = 0


class ActionPreview(Contract):
    """GET …/preview before a dangerous op (§41, §98): what changes, what is interrupted, how to undo."""
    title: str = ""
    changes: list[str] = []
    interrupts: list[str] = []
    rollback: str = ""
    confirm: ConfirmKind = "simple"
    confirm_text: str | None = None         # the exact text the user must type when confirm == "typed"


class ModelActionRequest(Contract):
    """POST /api/models/deployments/{id}/{action}."""
    confirm: str | None = None
    alias: str | None = None
    percent: int | None = None


class RollbackRequest(Contract):
    """POST /api/models/roles/{role}/rollback (typed confirmation)."""
    confirm: str = ""


class DiscoveryRequest(Contract):
    """POST /api/models/discovery."""
    categories: list[str] | None = None


# ── jobs (§30, §31) ───────────────────────────────────────────────────────────────────────────

JobKind = Literal["batch", "discovery", "download", "benchmark", "evaluation", "backup", "maintenance", "agent"]
JobStatus = Literal["running", "queued", "waiting", "paused", "completed", "failed", "cancelled"]
JobAction = Literal["pause", "resume", "cancel", "retry"]


class JobCounts(Contract):
    done: int = 0                           # items that succeeded (failed items are not included)
    total: int = 0
    failed: int = 0


class Job(Contract):
    """Any background work. id is '<source>:<native id>' (batch:bj_x, discovery:<run>, download:<mid>…)."""
    id: str = ""
    kind: JobKind = "batch"
    title: str = ""
    status: JobStatus = "queued"
    status_label: str = ""                  # "Paused", "Waiting for the primary workload"
    reason: str | None = None               # "GPU reserved for the primary workload" (§31)
    resumes_automatically: bool = False
    progress: float | None = None           # 0..1
    counts: JobCounts | None = None
    started_at: float | None = None
    finished_at: float | None = None
    owner: str | None = None
    actions: list[JobAction] = []
    tech: list[TechDetail] = []


class JobsSummary(Contract):
    running: int = 0
    queued: int = 0
    waiting: int = 0
    failed_24h: int = 0


class JobGroups(Contract):
    running: list[Job] = []
    queued: list[Job] = []
    waiting: list[Job] = []                 # includes paused
    completed: list[Job] = []
    failed: list[Job] = []


class JobsResponse(Contract):
    """GET /api/jobs."""
    summary: JobsSummary = JobsSummary()
    groups: JobGroups = JobGroups()
    batch_paused: bool = False
    note: str | None = None                 # e.g. "Batch service unreachable: showing controller work only"


class PauseAllRequest(Contract):
    """POST /api/jobs/batch/pause-all."""
    paused: bool = True


# ── decisions, agents, approvals (§22–§25, §40) ──────────────────────────────────────────────

class DecisionBadge(Contract):
    """'Decision: Use fast local model · Confidence 97%' with an expandable 'Why?' (§25). Never chain-of-thought."""
    decision: str = ""
    confidence: float | None = None         # 0..1
    decided_by: DecidedBy = "rules"
    actionable: bool = True
    why: list[Fact] = []


class TimelineStep(Contract):
    """One line of an agent's activity timeline (§24)."""
    ts: float = 0.0
    label: str = ""
    detail: str | None = None
    kind: Literal["info", "success", "warning", "error", "decision"] = "info"


class ArtifactLink(Contract):
    label: str = ""
    href: str | None = None


class AgentRun(Contract):
    """Agent work, synthesized from real sources (discovery runs, benchmarks, decision cycle, traces)."""
    id: str = ""
    agent: str = ""                         # "Model Scout", "Evaluator", "Decision Tuner"
    task: str = ""
    status: Literal["running", "done", "failed", "waiting_approval", "queued"] = "queued"
    started_at: float | None = None
    finished_at: float | None = None
    progress: float | None = None           # 0..1
    current_step: str | None = None
    steps: list[TimelineStep] = []
    decisions: list[DecisionBadge] = []
    tools: list[str] = []
    artifacts: list[ArtifactLink] = []
    cost_label: str | None = None
    result: str | None = None
    source: Literal["discovery", "benchmark", "decision_cycle", "trace"] = "discovery"
    tech: list[TechDetail] = []


class AgentParamOption(Contract):
    value: str = ""
    label: str = ""


class AgentParam(Contract):
    key: str = ""
    label: str = ""
    type: Literal["text", "select", "multiselect", "candidate"] = "text"
    options: list[AgentParamOption] | None = None
    required: bool = False


class AgentCatalogItem(Contract):
    """Something the user can start from the Agents page. runnable=False comes with why_not."""
    id: str = ""                            # "model-scout", "evaluator"
    name: str = ""
    description: str = ""
    runnable: bool = False
    why_not: str | None = None
    perm: Perm = "models.discover"
    params: list[AgentParam] = []


class AgentsOverview(Contract):
    """GET /api/agents."""
    running: list[AgentRun] = []
    recent: list[AgentRun] = []
    catalog: list[AgentCatalogItem] = []
    note: str | None = None


class AgentRunRequest(Contract):
    """POST /api/agents/run."""
    agent: str = ""
    params: dict[str, str | list[str]] = {}


class ApprovalOption(Contract):
    value: str = ""
    label: str = ""
    description: str | None = None
    tone: Literal["primary", "danger", "neutral"] = "neutral"


class Approval(Contract):
    """Something waiting on a human (§40): a decision review ticket, a device pairing, or a gated action."""
    id: str = ""                            # "review:<ticket>" | "pairing:<id>"
    kind: Literal["review", "device_pairing", "action"] = "review"
    title: str = ""
    action: str = ""
    why: str = ""
    impact: str = ""                        # reviews: "Nothing is waiting on this answer"
    options: list[ApprovalOption] = []
    created_at: float = 0.0
    status: Literal["pending", "approved", "rejected", "answered", "expired"] = "pending"
    blocking: bool = False
    tech: list[TechDetail] = []


class ApprovalAnswer(Contract):
    """POST /api/approvals/{id}."""
    answer: str = ""


# ── notifications, services, system status (§2, §6, §35, §38, §39) ───────────────────────────

NotificationKind = Literal["fallback", "memory_pressure", "candidate", "approval", "service_down",
                           "blerbz_contention", "job_done"]
ServiceAction = Literal["retry", "restart", "use_fallback", "view_logs"]


class Notification(Contract):
    """Only for things that may need the user (§39, §100). Not the DOM Notification API."""
    id: str = ""
    ts: float = 0.0
    severity: Severity = "info"
    title: str = ""
    body: str = ""
    href: str | None = None
    kind: NotificationKind = "fallback"


class ServiceHealth(Contract):
    """A platform service in human terms (§38, §91); the k8s state lives in tech."""
    key: str = ""                           # gateway|controller|batch|decision|knowledge|prometheus|gpusched|model:<profile id>
    name: str = ""                          # "AI gateway"
    health: Health = "unknown"
    summary: str = ""                       # "Service repeatedly failed to start"
    impact: str | None = None
    actions: list[ServiceAction] = []
    tech: list[TechDetail] = []


class ConnectionInfo(Contract):
    local: bool = True
    secure: bool = False
    url: str = ""


class SystemStatus(Contract):
    """Home's status block (§6): answerable in five seconds (§110). Served from the poller cache."""
    health: Health = "unknown"
    headline: str = ""                      # "Labzilla is healthy" / "Running on fallback model"
    local_ai: Health = "unknown"
    local_ai_label: str = ""                # "Local AI Ready"
    primary_model: str | None = None
    fast_model: str | None = None
    resource: ResourceState = ResourceState()
    agents_running: int = 0
    jobs: JobsSummary = JobsSummary()
    approvals_pending: int = 0
    notifications: list[Notification] = []
    services: list[ServiceHealth] = []
    maintenance: bool = False
    batch_paused: bool = False
    connection: ConnectionInfo = ConnectionInfo()
    updated_at: float = 0.0
    tech: list[TechDetail] = []


class StorageItem(Contract):
    key: str = ""
    label: str = ""                         # "Model store", "Console database", "Root disk"
    health: Health = "unknown"
    used_gb: float | None = None
    total_gb: float | None = None
    count: int | None = None
    note: str = ""                          # "Registry bookkeeping, not a disk measurement"
    tech: list[TechDetail] = []


class StorageSummary(Contract):
    """GET /api/system/storage."""
    items: list[StorageItem] = []
    note: str | None = None


class LogsResponse(Contract):
    """GET /api/system/logs: relevant events first (§80); raw logs are honest about availability."""
    events: list[ActivityEvent] = []
    raw_logs_available: bool = False
    raw_logs_note: str = ""


class Alert(Contract):
    """A firing alert from Prometheus, in human words."""
    id: str = ""
    name: str = ""
    severity: Severity = "warning"
    summary: str = ""
    since: float | None = None
    tech: list[TechDetail] = []


class AlertsResponse(Contract):
    """GET /api/system/alerts."""
    alerts: list[Alert] = []
    available: bool = True
    reason: str | None = None


SettingKey = Literal["maintenance", "batch_paused", "automatic_discovery", "automatic_download",
                     "automatic_promotion", "discovery_disabled", "jev_disabled", "reserve_gpu_mib"]


class SettingItem(Contract):
    """One operator switch, with its consequence stated before it is flipped (§110)."""
    key: SettingKey = "maintenance"
    label: str = ""
    description: str = ""
    consequence: str = ""                   # what flipping it does right now
    kind: Literal["toggle", "number"] = "toggle"
    value: bool | float | None = None
    unit: str | None = None
    perm: Perm = "system.settings"
    dangerous: bool = False


class SettingsView(Contract):
    """GET /api/system/settings."""
    items: list[SettingItem] = []
    available: bool = True
    reason: str | None = None


class SettingUpdate(Contract):
    """POST /api/system/settings."""
    key: SettingKey = "maintenance"
    value: bool | float = False


# ── Ask (§9–§11, §18–§21, §64, §67) ───────────────────────────────────────────────────────────

AttachmentKind = Literal["text", "code", "log", "markdown", "json", "csv", "pdf", "image", "document", "other"]


class HumanErrorAction(Contract):
    """action is a client verb ("retry", "details", "login", "reload") or an app path starting with '/'."""
    label: str = ""
    action: str = ""


class HumanError(Contract):
    """What failed, what it means, what to do next (§81). Never 'HTTP 503'."""
    title: str = ""                         # "Default model unavailable"
    impact: str = ""                        # "Labzilla is using the fast fallback model…"
    next_step: str = ""
    actions: list[HumanErrorAction] = []
    tech: list[TechDetail] = []


class ErrorBody(Contract):
    """Every non-2xx /api response body."""
    error: HumanError = HumanError()


class RouteStep(Contract):
    """One hop of 'Auto ↓ Jev ↓ Local Reasoning' (§11)."""
    label: str = ""
    kind: Literal["mode", "decision", "model", "external"] = "model"


class TokenUsage(Contract):
    prompt: int = 0
    completion: int = 0


class WebSource(Contract):
    """One cited web source: the answer refers to it as [n]."""
    n: int = 0
    title: str = ""
    url: str = ""


class WebLookup(Contract):
    """The live lookup behind an answer: the one query that left the box, and what came back."""
    status: Literal["ok", "empty", "error", "blocked", "not_needed"] = "not_needed"
    query: str | None = None                # exactly what was sent to search
    sources: list[WebSource] = []
    providers: list[str] = []               # espn, open-meteo, searxng
    ms: float | None = None
    note: str | None = None                 # why nothing usable came back, in plain words
    by_guard: bool = False                  # looked up after the model claimed its data was out of date


class Receipt(Contract):
    """'Handled by: local/default · Qwen… · 1.4 sec' plus everything behind 'Open Details' (§9, §21)."""
    alias: str = ""
    role_label: str = ""
    served_by: str | None = None
    model_name: str | None = None
    physical_model: str | None = None
    fallback: bool = False
    degraded: bool = False
    reason_label: str | None = None
    latency_ms: float | None = None
    privacy: PrivacyUsed = "local_only"
    route: list[RouteStep] = []
    decision: DecisionBadge | None = None
    request_id: str | None = None
    tokens: TokenUsage | None = None
    clamped: bool = False                   # answer length capped while an external workload is busy
    web: WebLookup | None = None            # set when the gateway looked the question up
    tech: list[TechDetail] = []


class Attachment(Contract):
    """A file as stored on a message: included=False when it could not be processed locally (note says why)."""
    name: str = ""
    kind: AttachmentKind = "other"
    size: int = 0
    included: bool = False
    note: str | None = None
    width: int | None = None                # images: pixel size as sent (the image itself is never stored)
    height: int | None = None


class AttachmentIn(Contract):
    """A file as sent by the client: text is extracted in the browser (≤ 256 KB). An image is downscaled in the
    browser (≤ 1024 px JPEG) and sent once as a data URL for the vision model; the server never stores it."""
    name: str = ""
    kind: AttachmentKind = "other"
    size: int = 0
    text: str | None = None
    image: str | None = None                # "data:image/jpeg;base64,…" (jpeg, png or webp; ≤ 1.5 MB decoded)
    width: int | None = None
    height: int | None = None


class Message(Contract):
    id: str = ""
    role: Literal["user", "assistant", "system"] = "user"
    content: str = ""
    created_at: float = 0.0
    status: Literal["streaming", "done", "error", "cancelled"] = "done"
    error: HumanError | None = None
    receipt: Receipt | None = None
    attachments: list[Attachment] = []


class Thread(Contract):
    """Server-side conversation, visible from every paired device (§68–§70)."""
    id: str = ""
    title: str = ""
    mode: AskMode = "auto"
    privacy: PrivacyChoice = "local_only"
    created_at: float = 0.0
    updated_at: float = 0.0
    origin_device: str | None = None
    messages: list[Message] = []


class ThreadSummary(Contract):
    """Prompt history row, grouped Today / Yesterday / Earlier (§67)."""
    id: str = ""
    title: str = ""
    updated_at: float = 0.0
    preview: str = ""
    mode: AskMode = "auto"
    group: Literal["today", "yesterday", "earlier"] = "today"
    origin_device: str | None = None        # paired device that started it ("iPhone"); None = an admin browser
    active: bool = False                    # an answer is being written right now (on any device)


class RenameThreadRequest(Contract):
    """PATCH /api/ai/threads/{id}."""
    title: str = ""


class CreateThreadRequest(Contract):
    """POST /api/ai/threads."""
    mode: AskMode = "auto"
    privacy: PrivacyChoice = "local_only"
    title: str | None = None


class MessageRequest(Contract):
    """POST /api/ai/threads/{id}/messages → text/event-stream of AskStreamPayloads."""
    content: str = ""
    mode: AskMode = "auto"
    privacy: PrivacyChoice = "local_only"
    attachments: list[AttachmentIn] = []
    tz: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_+\-/]*$")   # browser's IANA zone


class StreamRoute(Contract):
    route: list[RouteStep] = []
    privacy: PrivacyUsed = "local_only"
    message_id: str | None = None           # the assistant message being written (Stop can cancel at once)


class StreamDelta(Contract):
    text: str = ""


class StreamDone(Contract):
    message_id: str = ""


class StreamPhase(Contract):
    """Before the first word: "waiting" until the gateway has a model slot for this answer, then "reading"
    while the model processes the prompt (the whole conversation minus what it had cached). Long prompts on
    the CPU tier take tens of seconds here, so the answer shows progress instead of an empty bubble."""
    phase: Literal["waiting", "reading"] = "waiting"
    total: int | None = None                 # prompt tokens
    cached: int | None = None                # reused from the model's prompt cache (no work)
    processed: int | None = None             # of total, done so far (llama.cpp reports per batch)


AskStreamEvent = Literal["route", "phase", "delta", "receipt", "error", "done"]


class AskStreamPayloads(Contract):
    """Type map, not a response: the `data` of each SSE event on the Ask stream, keyed by event name."""
    route: StreamRoute = StreamRoute()
    phase: StreamPhase = StreamPhase()
    delta: StreamDelta = StreamDelta()
    receipt: Receipt = Receipt()
    error: HumanError = HumanError()
    done: StreamDone = StreamDone()


class ModeOption(Contract):
    mode: AskMode = "auto"
    label: str = ""                         # "Fast"
    blurb: str = ""                         # "Best for low-latency requests."
    alias: str = ""                         # "local/fast"
    available: bool = True
    reason: str | None = None


class PrivacyOption(Contract):
    value: PrivacyChoice = "local_only"
    label: str = ""                         # "Local only" | "Allow Jev routing"
    blurb: str = ""
    available: bool = True
    reason: str | None = None


class AiCapabilities(Contract):
    """GET /api/ai/capabilities. External models stay off until privacy policy allows them (§65)."""
    modes: list[ModeOption] = []
    privacy_options: list[PrivacyOption] = []
    external_allowed: bool = False
    external_note: str = "External models: off (privacy policy)"


# ── command bar (§7, §8, §50) ────────────────────────────────────────────────────────────────

CommandKind = Literal["ai_prompt", "system_query", "agent_request", "model_request", "knowledge_query",
                      "operational_command", "navigation"]


class ProposedAction(Contract):
    """An action the command bar suggests. The client runs it only after the user presses the button."""
    id: str = ""
    label: str = ""                         # "Pause all batch jobs"
    description: str = ""
    impact: str = ""                        # stated before triggering (§110)
    reversible: bool = True
    confirm: ConfirmKind = "none"
    confirm_text: str | None = None
    perm: Perm = "read"
    method: Literal["POST", "DELETE"] = "POST"
    path: str = ""                          # an /api path from the allowlisted surface
    body: dict[str, Any] = {}


class PromptSuggestion(Contract):
    text: str = ""
    mode: AskMode = "auto"


class CommandRequest(Contract):
    """POST /api/command."""
    text: str = ""


class CommandResolution(Contract):
    """What the command bar understood and what it can do about it (§8)."""
    kind: CommandKind = "ai_prompt"
    confidence: float = 0.0
    title: str = ""
    answer: str | None = None               # markdown-lite (paragraphs, **bold**, `code`, - lists)
    facts: list[Fact] = []
    proposed_action: ProposedAction | None = None
    navigate: str | None = None
    prompt: PromptSuggestion | None = None  # set for ai_prompt: hand off to Ask
    decided_by: Literal["rules", "local_model"] = "rules"


# ── knowledge (§32–§34) ──────────────────────────────────────────────────────────────────────

class KnowledgeLink(Contract):
    rel: str = ""                           # "supported by", "depends on", "affects"
    key: str = ""
    title: str = ""


class KnowledgeHit(Contract):
    key: str = ""
    type: str = ""                          # decision, assumption, evidence, project, incident, change…
    title: str = ""
    summary: str = ""
    status: str | None = None               # human label ("In effect", "Holding")
    updated: str | None = None              # ISO date
    links: list[KnowledgeLink] = []


class EvidenceRef(Contract):
    key: str = ""
    title: str = ""
    summary: str | None = None


class AssumptionRef(Contract):
    key: str = ""
    title: str = ""
    status: str = ""


class HistoryEntry(Contract):
    ts: str = ""                            # ISO date
    change: str = ""


class DecisionRecord(Contract):
    """A readable decision record (§34)."""
    key: str = ""
    title: str = ""
    status: str = ""                        # "In effect"
    question: str | None = None
    decision: str = ""
    why: list[str] = []
    evidence: list[EvidenceRef] = []
    assumptions: list[AssumptionRef] = []
    affected: list[str] = []
    alternatives: list[str] = []
    history: list[HistoryEntry] = []
    reconsideration: str | None = None


class KnowledgeObjectView(Contract):
    """Any non-decision knowledge object, readable."""
    key: str = ""
    type: str = ""
    title: str = ""
    status: str | None = None
    summary: str = ""
    body: str = ""                          # markdown-lite; no absolute paths
    fields: list[Fact] = []
    links: list[KnowledgeLink] = []
    updated: str | None = None


class KnowledgeObjectResponse(Contract):
    """GET /api/knowledge/objects/{key}: exactly one of decision / object is set, per kind."""
    kind: Literal["decision", "object"] = "object"
    decision: DecisionRecord | None = None
    object: KnowledgeObjectView | None = None


class KnowledgeHome(Contract):
    """GET /api/knowledge: project memory, not a graph database (§32)."""
    projects: list[KnowledgeHit] = []
    recent_changes: list[KnowledgeHit] = []
    decisions: list[KnowledgeHit] = []
    assumptions: list[KnowledgeHit] = []
    needs_review: list[KnowledgeHit] = []
    connected: bool = False
    mode: Literal["service", "bundled", "unavailable"] = "unavailable"
    note: str | None = None                 # "Read-only: bundled public knowledge"


class NoteRequest(Contract):
    """POST /api/knowledge/notes ('Save to Knowledge')."""
    title: str = ""
    body: str = ""
    source_thread: str | None = None


# ── identity, devices, setup, access (§14–§17, §83) ──────────────────────────────────────────

class User(Contract):
    id: str = ""
    name: str = ""
    role: UserRole = "device"
    device_name: str | None = None
    perms: list[Perm] = []


class Device(Contract):
    id: str = ""
    name: str = ""
    paired_at: float = 0.0
    last_seen: float | None = None
    user_agent_summary: str = ""            # "Safari on iPhone"
    current: bool = False


class Pairing(Contract):
    """Device pairing (§17). token/url only go to the admin who started it; the code shows on both screens."""
    id: str = ""
    url: str = ""                           # <public_url>/pair#<token>
    token: str | None = None
    expires_at: float = 0.0
    status: Literal["waiting", "claimed", "approved", "rejected", "expired"] = "waiting"
    code: str | None = None                 # 6-digit verification code, after claim
    device_name: str | None = None


class PairClaimRequest(Contract):
    """POST /api/pair/claim."""
    token: str = ""
    device_name: str = ""


class LoginRequest(Contract):
    """POST /api/auth/login."""
    name: str = ""
    passphrase: str = ""


class DetectedItem(Contract):
    label: str = ""                         # "DGX Spark", "K3s", "GPU", "Network"
    value: str = ""
    ok: bool = False


class SetupState(Contract):
    """GET /api/setup (§83)."""
    needs_setup: bool = False
    setup_code_configured: bool = False
    detected: list[DetectedItem] = []
    public_url: str = ""
    alt_urls: list[str] = []
    lan_url: str | None = None


class SetupRequest(Contract):
    """POST /api/setup."""
    setup_code: str = ""
    name: str = ""
    passphrase: str = ""
    default_mode: AskMode = "auto"


class AccessInfo(Contract):
    """GET /api/access: how to reach and trust this console (§14, §63, §78)."""
    public_url: str = ""
    alt_urls: list[str] = []
    lan_url: str | None = None
    secure: bool = False
    trusted_hint: str = ""
    mdns: Literal["published", "not_published", "unknown"] = "unknown"
    ca_available: bool = False               # GET /api/trust/ca.crt serves the Labzilla Local CA certificate
    ca_sha256: str | None = None             # its SHA-256 fingerprint ("AB:CD:…"), to check after downloading


class OkResponse(Contract):
    """Generic acknowledgement for mutations that return nothing richer."""
    ok: bool = True
    message: str | None = None


# ── live events (/api/events, §61) ───────────────────────────────────────────────────────────

EventType = Literal["status", "activity", "jobs", "approval", "notification", "thread", "pairing", "model"]


class JobsEvent(Contract):
    summary: JobsSummary = JobsSummary()
    job: Job | None = None                  # the job that changed, when there is exactly one


class ThreadEvent(Contract):
    """Live Ask history for every session of the owner (§67–§70).
    kind "message": partial assistant output (~1 s cadence) so another device can follow a running answer.
    kind "upsert": the conversation was created, renamed, got a new prompt or finished; `summary` is its new row.
    kind "deleted": the conversation is gone (deleted on a device, or removed by the storage cap)."""
    kind: Literal["message", "upsert", "deleted"] = "message"
    thread_id: str = ""
    message_id: str = ""
    content: str = ""
    status: Literal["streaming", "done", "error", "cancelled"] = "streaming"
    summary: ThreadSummary | None = None


class ModelEvent(Contract):
    role: RoleKey | None = None
    deployment_id: str | None = None
    summary: str = ""


class EventPayloads(Contract):
    """Type map, not a response: the `data` of each /api/events SSE event, keyed by event type."""
    status: SystemStatus = SystemStatus()
    activity: ActivityEvent = ActivityEvent()
    jobs: JobsEvent = JobsEvent()
    approval: Approval = Approval()
    notification: Notification = Notification()
    thread: ThreadEvent = ThreadEvent()
    pairing: Pairing = Pairing()
    model: ModelEvent = ModelEvent()


# ── Home cockpit (§6): one call for everything beyond the status block ──────────────────────────────

class Trend(Contract):
    """A recent time series for a sparkline: `values[i]` is at `start + i*step`. null = no data (drawn as a
    gap, never as 0): no traffic, or the source was silent."""
    key: str = ""
    label: str = ""
    unit: str = ""                           # "tok/s", "s", "req/min", "GB", "%"
    start: float = 0.0
    step: float = 0.0
    values: list[float | None] = []
    latest: float | None = None              # the last non-null value
    threshold: float | None = None           # a line worth drawing (e.g. the 8 GB memory safety margin)
    threshold_label: str | None = None
    href: str = ""                           # drill-down


class AskStats(Contract):
    """This console's Ask answers in the last `window_hours` (not all gateway traffic: that is in Trends)."""
    window_hours: int = 24
    answers: int = 0
    failed: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    median_latency_ms: float | None = None   # request in → answer complete
    p90_latency_ms: float | None = None


class Availability(Contract):
    """Share of availability probes in which useful local AI answered (controller)."""
    last_24h: float | None = None
    last_7d: float | None = None
    target: float | None = None


class ValueStats(Contract):
    """Local AI value in the last 24 h (controller /v1/savings). Counts are measured; every $ is an ESTIMATE."""
    requests: int | None = None
    local_tokens: int | None = None
    llm_avoidance: float | None = None       # share of tasks solved without an LLM (code or Jev)
    api_equivalent_usd: float | None = None  # ESTIMATE: what the same tokens would cost on a hosted API
    net_savings_usd: float | None = None     # ESTIMATE: minus Jev spend and local power
    basis: str = ""


class HomeOverview(Contract):
    """GET /api/home: the cockpit beyond the status block. Each part says when it is unavailable."""
    generated_at: float = 0.0
    ask: AskStats = AskStats()
    threads: list[ThreadSummary] = []        # most recent first
    availability: Availability = Availability()
    value: ValueStats | None = None
    value_note: str | None = None            # why `value` is missing
    trends: list[Trend] = []
    trends_hours: int = 6
    trends_note: str | None = None           # why trends are missing
