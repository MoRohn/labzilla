// generated — do not edit. Source: lif/lif/console/contracts.py
// Regenerate: lif/.venv/bin/python -m lif.console.gen_ts

export type Health = "healthy" | "busy" | "degraded" | "paused" | "attention" | "offline" | "unknown";

export type Severity = "info" | "success" | "warning" | "error";

export type ConfirmKind = "none" | "simple" | "typed";

export type DecidedBy = "rules" | "jev" | "local_model" | "human" | "code";

export type Perm = "read" | "ask" | "jobs.control" | "approvals.answer" | "models.discover" | "models.operate" | "models.release" | "system.safe" | "system.settings" | "devices.manage";

export type UserRole = "admin" | "device";

export type PrivacyChoice = "local_only" | "allow_jev";

export type PrivacyUsed = "local_only" | "local_jev" | "external";

export type AskMode = "auto" | "fast" | "balanced" | "deep" | "code" | "vision";

export type RoleKey = "auto" | "fast" | "balanced" | "deep" | "code" | "vision" | "instant" | "batch" | "embedding" | "rerank" | "web";

/** One raw fact for the Technical Details drawer (k8s resource, pod, revision, raw reason, request id). */
export interface TechDetail {
  label: string;
  value: string;
}

/** A human label/value pair shown inline (command answers, decision 'why' rows). */
export interface Fact {
  label: string;
  value: string;
}

export type BlerbzState = "idle" | "busy" | "imminent" | "reserved" | "unknown";

export type Temperature = "normal" | "warm" | "hot" | "unknown";

export type WorkKey = "blerbz" | "ai_serving" | "other" | "available";

/** What the machine is doing. Memory is unified (CPU+GPU); None = not measurable right now. */
export interface ResourceState {
  /** false: no external workload; the console hides that row */
  primary_enabled: boolean;
  /** console label when primary_enabled (operator-configured) */
  primary_name: string;
  blerbz: BlerbzState;
  /** "Idle", "Running", "Reserved" */
  blerbz_label: string;
  /** one human sentence; raw scheduler reason lives in tech */
  blerbz_reason: string;
  gpu_util_pct?: number | null;
  mem_total_gb?: number | null;
  mem_used_gb?: number | null;
  mem_available_gb?: number | null;
  blerbz_mem_gb?: number | null;
  ai_serving_mem_gb?: number | null;
  other_mem_gb?: number | null;
  temperature: Temperature;
  /** e.g. "No GPU temperature sensor is exported yet" */
  temperature_note: string;
  stale: boolean;
  updated_at: number;
  tech: TechDetail[];
}

/** One segment of 'Current work' (§36): primary workload 18%, AI Serving 24%, Available 58%. */
export interface WorkShare {
  key: WorkKey;
  label: string;
  gb?: number | null;
  pct?: number | null;
}

/** GET /api/system/compute. */
export interface ComputeView {
  device: string;
  resource: ResourceState;
  work: WorkShare[];
  /** e.g. "Memory split needs Prometheus; showing totals only" */
  note: string;
  tech: TechDetail[];
}

/** One hour of the workload timeline (§37). */
export interface TimelineBucket {
  start: number;
  end: number;
  /** "inference", "idle", "GPU", "inference + batch" */
  label: string;
  blerbz_pct?: number | null;
  ai_pct?: number | null;
  batch_items?: number | null;
}

/** GET /api/system/timeline. available=False + reason when Prometheus can't answer. */
export interface TimelineResponse {
  hours: number;
  buckets: TimelineBucket[];
  available: boolean;
  reason?: string | null;
  primary_enabled: boolean;
  primary_name: string;
}

export type RoleCause = "canary" | "primary_unavailable" | "below_quality_floor" | "not_deployed" | "shed_by_memory_guard" | "yielded_to_primary" | "unknown";

export type MemoryBasis = "configured" | "estimated" | "measured";

export type SpeedBasis = "measured" | "estimated" | "live";

export type ModelAction = "load" | "unload" | "benchmark" | "download" | "test" | "promote" | "canary" | "delete" | "block" | "unblock" | "pin" | "unpin";

export type PreviewAction = "promote" | "canary" | "rollback" | "unload" | "delete" | "load";

export interface CanaryInfo {
  model: string;
  percent: number;
}

/** A logical capability (local/fast, local/default…) and who serves it right now. */
export interface ModelRole {
  role: RoleKey;
  /** "local/fast" */
  alias: string;
  /** "Fast" */
  label: string;
  /** "Best for low-latency requests." (§96) */
  blurb: string;
  state: Health;
  /** "Ready" | "Standby" | "On demand" | "Offline" | "Degraded" */
  state_label: string;
  /** friendly name of what serves it now */
  model_name: string;
  /** repo@revision, for "View physical model" (§10) */
  physical_model?: string | null;
  /** profile id */
  served_by?: string | null;
  fallback_active: boolean;
  degraded: boolean;
  cause?: RoleCause | null;
  /** "Default model paused to free memory for the primary workload" */
  cause_label?: string | null;
  /** friendly names, primary first */
  chain: string[];
  canary?: CanaryInfo | null;
  tech: TechDetail[];
}

export interface BenchmarkSummary {
  ts: number;
  suite: string;
  /** 0..1 pass rate */
  quality?: number | null;
  ttft_ms_p50?: number | null;
  decode_tps_p50?: number | null;
  latency_ms_p50?: number | null;
  errors: number;
  items: number;
}

/** A physical model the platform knows about (production/standby on /api/models; candidates elsewhere). */
export interface ModelDeployment {
  /** registry profile id */
  id: string;
  /** friendly */
  name: string;
  /** Hugging Face repo */
  model_id: string;
  revision: string;
  source: string;
  /** "llama.cpp CPU" */
  runtime: string;
  device: string;
  precision: string;
  params_b?: number | null;
  context?: number | null;
  /** raw lifecycle state (PRODUCTION, STANDBY, CANDIDATE…) */
  state: string;
  /** "In use", "Kept as backup", "Shortlisted, not downloaded" */
  state_label: string;
  health: Health;
  /** aliases this model serves or is primary for */
  roles: string[];
  memory_gb?: number | null;
  memory_basis: MemoryBasis;
  speed_tps?: number | null;
  ttft_ms?: number | null;
  speed_basis?: SpeedBasis | null;
  quality?: number | null;
  /** None = never benchmarked */
  last_evaluated?: number | null;
  /** requests served in the last 24 h */
  usage_24h?: number | null;
  pinned: boolean;
  blocked: boolean;
  /** actions valid in this state (permission still checked server-side) */
  actions: ModelAction[];
  tech: TechDetail[];
}

/** One human-readable thing that happened (§6 Recent Activity, §80 relevant events). */
export interface ActivityEvent {
  id: string;
  ts: number;
  category: "model" | "agent" | "job" | "system" | "decision" | "security" | "knowledge";
  severity: Severity;
  title: string;
  detail?: string | null;
  href?: string | null;
  tech: TechDetail[];
}

/** GET /api/models/deployments/{id}. */
export interface DeploymentDetail {
  deployment: ModelDeployment;
  benchmarks: BenchmarkSummary[];
  activity: ActivityEvent[];
}

export interface ComparisonRow {
  /** "Quality", "Time to first token", "Throughput", "Memory", "Stability" */
  metric: string;
  current?: number | null;
  candidate?: number | null;
  unit: string;
  better: "higher" | "lower";
  verdict: "better" | "worse" | "same" | "unknown";
  /** "estimated", "not benchmarked yet" */
  note?: string | null;
}

/** Side-by-side current vs candidate for one role (§29). */
export interface CandidateComparison {
  candidate: ModelDeployment;
  incumbent?: ModelDeployment | null;
  /** alias the candidate would serve */
  role: string;
  rows: ComparisonRow[];
  /** human sentence */
  recommendation: string;
  recommendation_code: "canary" | "hold" | "reject" | "benchmark_first" | "unknown";
  /** Jev advisory, labelled as advice */
  advisory?: string | null;
  tech: TechDetail[];
}

export interface CandidateItem {
  deployment: ModelDeployment;
  /** "Faster than Fast's current model; not benchmarked yet" */
  comparison_hint: string;
  role?: RoleKey | null;
}

export interface DiscoveryStage {
  key: "discovering" | "filtering" | "evaluating" | "benchmarking";
  /** "Discovering (2,000 models found)" */
  label: string;
  count?: number | null;
  done: boolean;
}

/** A 'Check for better models' run (§28). Stage counts appear only when the run finishes. */
export interface DiscoveryRun {
  id: string;
  started_at: number;
  finished_at?: number | null;
  status: "running" | "succeeded" | "failed" | "interrupted";
  stages: DiscoveryStage[];
  /** "2 meaningful upgrades found" */
  summary: string;
  upgrades: number;
  categories: string[];
  tech: TechDetail[];
}

/** GET /api/models/discovery. */
export interface DiscoveryState {
  current?: DiscoveryRun | null;
  recent: DiscoveryRun[];
  enabled: boolean;
  /** why checks can't start (only meaningful when enabled is false) */
  disabled_reason?: string | null;
  /** non-blocking remark, e.g. the last request ended without a run */
  note?: string | null;
}

/** GET /api/models: the portfolio (roles first), not every downloaded file (§26). */
export interface ModelsOverview {
  roles: ModelRole[];
  deployments: ModelDeployment[];
  candidates_count: number;
}

/** GET …/preview before a dangerous op (§41, §98): what changes, what is interrupted, how to undo. */
export interface ActionPreview {
  title: string;
  changes: string[];
  interrupts: string[];
  rollback: string;
  confirm: ConfirmKind;
  /** the exact text the user must type when confirm == "typed" */
  confirm_text?: string | null;
}

/** POST /api/models/deployments/{id}/{action}. */
export interface ModelActionRequest {
  confirm?: string | null;
  alias?: string | null;
  percent?: number | null;
}

/** POST /api/models/roles/{role}/rollback (typed confirmation). */
export interface RollbackRequest {
  confirm: string;
}

/** POST /api/models/discovery. */
export interface DiscoveryRequest {
  categories?: string[] | null;
}

export type JobKind = "batch" | "discovery" | "download" | "benchmark" | "evaluation" | "backup" | "maintenance" | "agent";

export type JobStatus = "running" | "queued" | "waiting" | "paused" | "completed" | "failed" | "cancelled";

export type JobAction = "pause" | "resume" | "cancel" | "retry";

export interface JobCounts {
  /** items that succeeded (failed items are not included) */
  done: number;
  total: number;
  failed: number;
}

/** Any background work. id is '<source>:<native id>' (batch:bj_x, discovery:<run>, download:<mid>…). */
export interface Job {
  id: string;
  kind: JobKind;
  title: string;
  status: JobStatus;
  /** "Paused", "Waiting for the primary workload" */
  status_label: string;
  /** "GPU reserved for the primary workload" (§31) */
  reason?: string | null;
  resumes_automatically: boolean;
  /** 0..1 */
  progress?: number | null;
  counts?: JobCounts | null;
  started_at?: number | null;
  finished_at?: number | null;
  owner?: string | null;
  actions: JobAction[];
  tech: TechDetail[];
}

export interface JobsSummary {
  running: number;
  queued: number;
  waiting: number;
  failed_24h: number;
}

export interface JobGroups {
  running: Job[];
  queued: Job[];
  /** includes paused */
  waiting: Job[];
  completed: Job[];
  failed: Job[];
}

/** GET /api/jobs. */
export interface JobsResponse {
  summary: JobsSummary;
  groups: JobGroups;
  batch_paused: boolean;
  /** e.g. "Batch service unreachable: showing controller work only" */
  note?: string | null;
}

/** POST /api/jobs/batch/pause-all. */
export interface PauseAllRequest {
  paused: boolean;
}

/** 'Decision: Use fast local model · Confidence 97%' with an expandable 'Why?' (§25). Never chain-of-thought. */
export interface DecisionBadge {
  decision: string;
  /** 0..1 */
  confidence?: number | null;
  decided_by: DecidedBy;
  actionable: boolean;
  why: Fact[];
}

/** One line of an agent's activity timeline (§24). */
export interface TimelineStep {
  ts: number;
  label: string;
  detail?: string | null;
  kind: "info" | "success" | "warning" | "error" | "decision";
}

export interface ArtifactLink {
  label: string;
  href?: string | null;
}

/** Agent work, synthesized from real sources (discovery runs, benchmarks, decision cycle, traces). */
export interface AgentRun {
  id: string;
  /** "Model Scout", "Evaluator", "Decision Tuner" */
  agent: string;
  task: string;
  status: "running" | "done" | "failed" | "waiting_approval" | "queued";
  started_at?: number | null;
  finished_at?: number | null;
  /** 0..1 */
  progress?: number | null;
  current_step?: string | null;
  steps: TimelineStep[];
  decisions: DecisionBadge[];
  tools: string[];
  artifacts: ArtifactLink[];
  cost_label?: string | null;
  result?: string | null;
  source: "discovery" | "benchmark" | "decision_cycle" | "trace";
  tech: TechDetail[];
}

export interface AgentParamOption {
  value: string;
  label: string;
}

export interface AgentParam {
  key: string;
  label: string;
  type: "text" | "select" | "multiselect" | "candidate";
  options?: AgentParamOption[] | null;
  required: boolean;
}

/** Something the user can start from the Agents page. runnable=False comes with why_not. */
export interface AgentCatalogItem {
  /** "model-scout", "evaluator" */
  id: string;
  name: string;
  description: string;
  runnable: boolean;
  why_not?: string | null;
  perm: Perm;
  params: AgentParam[];
}

/** GET /api/agents. */
export interface AgentsOverview {
  running: AgentRun[];
  recent: AgentRun[];
  catalog: AgentCatalogItem[];
  note?: string | null;
}

/** POST /api/agents/run. */
export interface AgentRunRequest {
  agent: string;
  params: Record<string, string | string[]>;
}

export interface ApprovalOption {
  value: string;
  label: string;
  description?: string | null;
  tone: "primary" | "danger" | "neutral";
}

/** Something waiting on a human (§40): a decision review ticket, a device pairing, or a gated action. */
export interface Approval {
  /** "review:<ticket>" | "pairing:<id>" */
  id: string;
  kind: "review" | "device_pairing" | "action";
  title: string;
  action: string;
  why: string;
  /** reviews: "Nothing is waiting on this answer" */
  impact: string;
  options: ApprovalOption[];
  created_at: number;
  status: "pending" | "approved" | "rejected" | "answered" | "expired";
  blocking: boolean;
  tech: TechDetail[];
}

/** POST /api/approvals/{id}. */
export interface ApprovalAnswer {
  answer: string;
}

export type NotificationKind = "fallback" | "memory_pressure" | "candidate" | "approval" | "service_down" | "blerbz_contention" | "job_done";

export type ServiceAction = "retry" | "restart" | "use_fallback" | "view_logs";

/** Only for things that may need the user (§39, §100). Not the DOM Notification API. */
export interface Notification {
  id: string;
  ts: number;
  severity: Severity;
  title: string;
  body: string;
  href?: string | null;
  kind: NotificationKind;
}

/** A platform service in human terms (§38, §91); the k8s state lives in tech. */
export interface ServiceHealth {
  /** gateway|controller|batch|decision|knowledge|prometheus|gpusched|model:<profile id> */
  key: string;
  /** "AI gateway" */
  name: string;
  health: Health;
  /** "Service repeatedly failed to start" */
  summary: string;
  impact?: string | null;
  actions: ServiceAction[];
  tech: TechDetail[];
}

export interface ConnectionInfo {
  local: boolean;
  secure: boolean;
  url: string;
}

/** Home's status block (§6): answerable in five seconds (§110). Served from the poller cache. */
export interface SystemStatus {
  health: Health;
  /** "Labzilla is healthy" / "Running on fallback model" */
  headline: string;
  local_ai: Health;
  /** "Local AI Ready" */
  local_ai_label: string;
  primary_model?: string | null;
  fast_model?: string | null;
  resource: ResourceState;
  agents_running: number;
  jobs: JobsSummary;
  approvals_pending: number;
  notifications: Notification[];
  services: ServiceHealth[];
  maintenance: boolean;
  batch_paused: boolean;
  connection: ConnectionInfo;
  updated_at: number;
  tech: TechDetail[];
}

export interface StorageItem {
  key: string;
  /** "Model store", "Console database", "Root disk" */
  label: string;
  health: Health;
  used_gb?: number | null;
  total_gb?: number | null;
  count?: number | null;
  /** "Registry bookkeeping, not a disk measurement" */
  note: string;
  tech: TechDetail[];
}

/** GET /api/system/storage. */
export interface StorageSummary {
  items: StorageItem[];
  note?: string | null;
}

/** GET /api/system/logs: relevant events first (§80); raw logs are honest about availability. */
export interface LogsResponse {
  events: ActivityEvent[];
  raw_logs_available: boolean;
  raw_logs_note: string;
}

/** A firing alert from Prometheus, in human words. */
export interface Alert {
  id: string;
  name: string;
  severity: Severity;
  summary: string;
  since?: number | null;
  tech: TechDetail[];
}

/** GET /api/system/alerts. */
export interface AlertsResponse {
  alerts: Alert[];
  available: boolean;
  reason?: string | null;
}

export type SettingKey = "maintenance" | "batch_paused" | "automatic_discovery" | "automatic_download" | "automatic_promotion" | "discovery_disabled" | "jev_disabled" | "reserve_gpu_mib";

/** One operator switch, with its consequence stated before it is flipped (§110). */
export interface SettingItem {
  key: SettingKey;
  label: string;
  description: string;
  /** what flipping it does right now */
  consequence: string;
  kind: "toggle" | "number";
  value?: boolean | number | null;
  unit?: string | null;
  perm: Perm;
  dangerous: boolean;
}

/** GET /api/system/settings. */
export interface SettingsView {
  items: SettingItem[];
  available: boolean;
  reason?: string | null;
}

/** POST /api/system/settings. */
export interface SettingUpdate {
  key: SettingKey;
  value: boolean | number;
}

export type AttachmentKind = "text" | "code" | "log" | "markdown" | "json" | "csv" | "pdf" | "image" | "document" | "other";

/** action is a client verb ("retry", "details", "login", "reload") or an app path starting with '/'. */
export interface HumanErrorAction {
  label: string;
  action: string;
}

/** What failed, what it means, what to do next (§81). Never 'HTTP 503'. */
export interface HumanError {
  /** "Default model unavailable" */
  title: string;
  /** "Labzilla is using the fast fallback model…" */
  impact: string;
  next_step: string;
  actions: HumanErrorAction[];
  tech: TechDetail[];
}

/** Every non-2xx /api response body. */
export interface ErrorBody {
  error: HumanError;
}

/** One hop of 'Auto ↓ Jev ↓ Local Reasoning' (§11). */
export interface RouteStep {
  label: string;
  kind: "mode" | "decision" | "model" | "external";
}

export interface TokenUsage {
  prompt: number;
  completion: number;
}

/** One cited web source: the answer refers to it as [n]. */
export interface WebSource {
  n: number;
  title: string;
  url: string;
}

/** The live lookup behind an answer: the one query that left the box, and what came back. */
export interface WebLookup {
  status: "ok" | "empty" | "error" | "blocked" | "not_needed";
  /** exactly what was sent to search */
  query?: string | null;
  sources: WebSource[];
  /** espn, open-meteo, searxng */
  providers: string[];
  ms?: number | null;
  /** why nothing usable came back, in plain words */
  note?: string | null;
  /** looked up after the model claimed its data was out of date */
  by_guard: boolean;
}

/** 'Handled by: local/default · Qwen… · 1.4 sec' plus everything behind 'Open Details' (§9, §21). */
export interface Receipt {
  alias: string;
  role_label: string;
  served_by?: string | null;
  model_name?: string | null;
  physical_model?: string | null;
  fallback: boolean;
  degraded: boolean;
  reason_label?: string | null;
  latency_ms?: number | null;
  privacy: PrivacyUsed;
  route: RouteStep[];
  decision?: DecisionBadge | null;
  request_id?: string | null;
  tokens?: TokenUsage | null;
  /** answer length capped while an external workload is busy */
  clamped: boolean;
  /** set when the gateway looked the question up */
  web?: WebLookup | null;
  tech: TechDetail[];
}

/** A file as stored on a message: included=False when it could not be processed locally (note says why). */
export interface Attachment {
  name: string;
  kind: AttachmentKind;
  size: number;
  included: boolean;
  note?: string | null;
  /** images: pixel size as sent (the image itself is never stored) */
  width?: number | null;
  height?: number | null;
}

/**
 * A file as sent by the client: text is extracted in the browser (≤ 256 KB). An image is downscaled in the
 * browser (≤ 1024 px JPEG) and sent once as a data URL for the vision model; the server never stores it.
 */
export interface AttachmentIn {
  name: string;
  kind: AttachmentKind;
  size: number;
  text?: string | null;
  /** "data:image/jpeg;base64,…" (jpeg, png or webp; ≤ 1.5 MB decoded) */
  image?: string | null;
  width?: number | null;
  height?: number | null;
}

export interface Message {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: number;
  status: "streaming" | "done" | "error" | "cancelled";
  error?: HumanError | null;
  receipt?: Receipt | null;
  attachments: Attachment[];
}

/** Server-side conversation, visible from every paired device (§68–§70). */
export interface Thread {
  id: string;
  title: string;
  mode: AskMode;
  privacy: PrivacyChoice;
  created_at: number;
  updated_at: number;
  origin_device?: string | null;
  messages: Message[];
}

/** Prompt history row, grouped Today / Yesterday / Earlier (§67). */
export interface ThreadSummary {
  id: string;
  title: string;
  updated_at: number;
  preview: string;
  mode: AskMode;
  group: "today" | "yesterday" | "earlier";
  /** paired device that started it ("iPhone"); None = an admin browser */
  origin_device?: string | null;
  /** an answer is being written right now (on any device) */
  active: boolean;
}

/** PATCH /api/ai/threads/{id}. */
export interface RenameThreadRequest {
  title: string;
}

/** POST /api/ai/threads. */
export interface CreateThreadRequest {
  mode: AskMode;
  privacy: PrivacyChoice;
  title?: string | null;
}

/** POST /api/ai/threads/{id}/messages → text/event-stream of AskStreamPayloads. */
export interface MessageRequest {
  content: string;
  mode: AskMode;
  privacy: PrivacyChoice;
  attachments: AttachmentIn[];
  /** browser's IANA zone */
  tz?: string | null;
}

export interface StreamRoute {
  route: RouteStep[];
  privacy: PrivacyUsed;
  /** the assistant message being written (Stop can cancel at once) */
  message_id?: string | null;
}

export interface StreamDelta {
  text: string;
}

export interface StreamDone {
  message_id: string;
}

/**
 * Before the first word: "waiting" until the gateway has a model slot for this answer, then "reading"
 * while the model processes the prompt (the whole conversation minus what it had cached). Long prompts on
 * the CPU tier take tens of seconds here, so the answer shows progress instead of an empty bubble.
 */
export interface StreamPhase {
  phase: "waiting" | "reading";
  /** prompt tokens */
  total?: number | null;
  /** reused from the model's prompt cache (no work) */
  cached?: number | null;
  /** of total, done so far (llama.cpp reports per batch) */
  processed?: number | null;
}

export type AskStreamEvent = "route" | "phase" | "delta" | "receipt" | "error" | "done";

/** Type map, not a response: the `data` of each SSE event on the Ask stream, keyed by event name. */
export interface AskStreamPayloads {
  route: StreamRoute;
  phase: StreamPhase;
  delta: StreamDelta;
  receipt: Receipt;
  error: HumanError;
  done: StreamDone;
}

export interface ModeOption {
  mode: AskMode;
  /** "Fast" */
  label: string;
  /** "Best for low-latency requests." */
  blurb: string;
  /** "local/fast" */
  alias: string;
  available: boolean;
  reason?: string | null;
}

export interface PrivacyOption {
  value: PrivacyChoice;
  /** "Local only" | "Allow Jev routing" */
  label: string;
  blurb: string;
  available: boolean;
  reason?: string | null;
}

/** GET /api/ai/capabilities. External models stay off until privacy policy allows them (§65). */
export interface AiCapabilities {
  modes: ModeOption[];
  privacy_options: PrivacyOption[];
  external_allowed: boolean;
  external_note: string;
}

export type CommandKind = "ai_prompt" | "system_query" | "agent_request" | "model_request" | "knowledge_query" | "operational_command" | "navigation";

/** An action the command bar suggests. The client runs it only after the user presses the button. */
export interface ProposedAction {
  id: string;
  /** "Pause all batch jobs" */
  label: string;
  description: string;
  /** stated before triggering (§110) */
  impact: string;
  reversible: boolean;
  confirm: ConfirmKind;
  confirm_text?: string | null;
  perm: Perm;
  method: "POST" | "DELETE";
  /** an /api path from the allowlisted surface */
  path: string;
  body: Record<string, unknown>;
}

export interface PromptSuggestion {
  text: string;
  mode: AskMode;
}

/** POST /api/command. */
export interface CommandRequest {
  text: string;
}

/** What the command bar understood and what it can do about it (§8). */
export interface CommandResolution {
  kind: CommandKind;
  confidence: number;
  title: string;
  /** markdown-lite (paragraphs, **bold**, `code`, - lists) */
  answer?: string | null;
  facts: Fact[];
  proposed_action?: ProposedAction | null;
  navigate?: string | null;
  /** set for ai_prompt: hand off to Ask */
  prompt?: PromptSuggestion | null;
  decided_by: "rules" | "local_model";
}

export interface KnowledgeLink {
  /** "supported by", "depends on", "affects" */
  rel: string;
  key: string;
  title: string;
}

export interface KnowledgeHit {
  key: string;
  /** decision, assumption, evidence, project, incident, change… */
  type: string;
  title: string;
  summary: string;
  /** human label ("In effect", "Holding") */
  status?: string | null;
  /** ISO date */
  updated?: string | null;
  links: KnowledgeLink[];
}

export interface EvidenceRef {
  key: string;
  title: string;
  summary?: string | null;
}

export interface AssumptionRef {
  key: string;
  title: string;
  status: string;
}

export interface HistoryEntry {
  /** ISO date */
  ts: string;
  change: string;
}

/** A readable decision record (§34). */
export interface DecisionRecord {
  key: string;
  title: string;
  /** "In effect" */
  status: string;
  question?: string | null;
  decision: string;
  why: string[];
  evidence: EvidenceRef[];
  assumptions: AssumptionRef[];
  affected: string[];
  alternatives: string[];
  history: HistoryEntry[];
  reconsideration?: string | null;
}

/** Any non-decision knowledge object, readable. */
export interface KnowledgeObjectView {
  key: string;
  type: string;
  title: string;
  status?: string | null;
  summary: string;
  /** markdown-lite; no absolute paths */
  body: string;
  fields: Fact[];
  links: KnowledgeLink[];
  updated?: string | null;
}

/** GET /api/knowledge/objects/{key}: exactly one of decision / object is set, per kind. */
export interface KnowledgeObjectResponse {
  kind: "decision" | "object";
  decision?: DecisionRecord | null;
  object?: KnowledgeObjectView | null;
}

/** GET /api/knowledge: project memory, not a graph database (§32). */
export interface KnowledgeHome {
  projects: KnowledgeHit[];
  recent_changes: KnowledgeHit[];
  decisions: KnowledgeHit[];
  assumptions: KnowledgeHit[];
  needs_review: KnowledgeHit[];
  connected: boolean;
  mode: "service" | "bundled" | "unavailable";
  /** "Read-only: bundled public knowledge" */
  note?: string | null;
}

/** POST /api/knowledge/notes ('Save to Knowledge'). */
export interface NoteRequest {
  title: string;
  body: string;
  source_thread?: string | null;
}

export interface User {
  id: string;
  name: string;
  role: UserRole;
  device_name?: string | null;
  perms: Perm[];
}

export interface Device {
  id: string;
  name: string;
  paired_at: number;
  last_seen?: number | null;
  /** "Safari on iPhone" */
  user_agent_summary: string;
  current: boolean;
}

/** Device pairing (§17). token/url only go to the admin who started it; the code shows on both screens. */
export interface Pairing {
  id: string;
  /** <public_url>/pair#<token> */
  url: string;
  token?: string | null;
  expires_at: number;
  status: "waiting" | "claimed" | "approved" | "rejected" | "expired";
  /** 6-digit verification code, after claim */
  code?: string | null;
  device_name?: string | null;
}

/** POST /api/pair/claim. */
export interface PairClaimRequest {
  token: string;
  device_name: string;
}

/** POST /api/auth/login. */
export interface LoginRequest {
  name: string;
  passphrase: string;
}

export interface DetectedItem {
  /** "DGX Spark", "K3s", "GPU", "Network" */
  label: string;
  value: string;
  ok: boolean;
}

/** GET /api/setup (§83). */
export interface SetupState {
  needs_setup: boolean;
  setup_code_configured: boolean;
  detected: DetectedItem[];
  public_url: string;
  alt_urls: string[];
  lan_url?: string | null;
}

/** POST /api/setup. */
export interface SetupRequest {
  setup_code: string;
  name: string;
  passphrase: string;
  default_mode: AskMode;
}

/** GET /api/access: how to reach and trust this console (§14, §63, §78). */
export interface AccessInfo {
  public_url: string;
  alt_urls: string[];
  lan_url?: string | null;
  secure: boolean;
  trusted_hint: string;
  mdns: "published" | "not_published" | "unknown";
  /** GET /api/trust/ca.crt serves the Labzilla Local CA certificate */
  ca_available: boolean;
  /** its SHA-256 fingerprint ("AB:CD:…"), to check after downloading */
  ca_sha256?: string | null;
}

/** Generic acknowledgement for mutations that return nothing richer. */
export interface OkResponse {
  ok: boolean;
  message?: string | null;
}

export type EventType = "status" | "activity" | "jobs" | "approval" | "notification" | "thread" | "pairing" | "model";

export interface JobsEvent {
  summary: JobsSummary;
  /** the job that changed, when there is exactly one */
  job?: Job | null;
}

/**
 * Live Ask history for every session of the owner (§67–§70).
 * kind "message": partial assistant output (~1 s cadence) so another device can follow a running answer.
 * kind "upsert": the conversation was created, renamed, got a new prompt or finished; `summary` is its new row.
 * kind "deleted": the conversation is gone (deleted on a device, or removed by the storage cap).
 */
export interface ThreadEvent {
  kind: "message" | "upsert" | "deleted";
  thread_id: string;
  message_id: string;
  content: string;
  status: "streaming" | "done" | "error" | "cancelled";
  summary?: ThreadSummary | null;
}

export interface ModelEvent {
  role?: RoleKey | null;
  deployment_id?: string | null;
  summary: string;
}

/** Type map, not a response: the `data` of each /api/events SSE event, keyed by event type. */
export interface EventPayloads {
  status: SystemStatus;
  activity: ActivityEvent;
  jobs: JobsEvent;
  approval: Approval;
  notification: Notification;
  thread: ThreadEvent;
  pairing: Pairing;
  model: ModelEvent;
}

/**
 * A recent time series for a sparkline: `values[i]` is at `start + i*step`. null = no data (drawn as a
 * gap, never as 0): no traffic, or the source was silent.
 */
export interface Trend {
  key: string;
  label: string;
  /** "tok/s", "s", "req/min", "GB", "%" */
  unit: string;
  start: number;
  step: number;
  values: (number | null)[];
  /** the last non-null value */
  latest?: number | null;
  /** a line worth drawing (e.g. the 8 GB memory safety margin) */
  threshold?: number | null;
  threshold_label?: string | null;
  /** drill-down */
  href: string;
}

/** This console's Ask answers in the last `window_hours` (not all gateway traffic: that is in Trends). */
export interface AskStats {
  window_hours: number;
  answers: number;
  failed: number;
  prompt_tokens: number;
  completion_tokens: number;
  /** request in → answer complete */
  median_latency_ms?: number | null;
  p90_latency_ms?: number | null;
}

/** Share of availability probes in which useful local AI answered (controller). */
export interface Availability {
  last_24h?: number | null;
  last_7d?: number | null;
  target?: number | null;
}

/** Local AI value in the last 24 h (controller /v1/savings). Counts are measured; every $ is an ESTIMATE. */
export interface ValueStats {
  requests?: number | null;
  local_tokens?: number | null;
  /** share of tasks solved without an LLM (code or Jev) */
  llm_avoidance?: number | null;
  /** ESTIMATE: what the same tokens would cost on a hosted API */
  api_equivalent_usd?: number | null;
  /** ESTIMATE: minus Jev spend and local power */
  net_savings_usd?: number | null;
  basis: string;
}

/** GET /api/home: the cockpit beyond the status block. Each part says when it is unavailable. */
export interface HomeOverview {
  generated_at: number;
  ask: AskStats;
  /** most recent first */
  threads: ThreadSummary[];
  availability: Availability;
  value?: ValueStats | null;
  /** why `value` is missing */
  value_note?: string | null;
  trends: Trend[];
  trends_hours: number;
  /** why trends are missing */
  trends_note?: string | null;
}
