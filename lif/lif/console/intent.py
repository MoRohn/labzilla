"""Command bar intent: deterministic rules that classify a sentence and build the answer (spec §7, §8, §50, §103).

`classify` maps text to a CommandKind with a confidence; `resolve` builds a CommandResolution, answering
system queries from the poller snapshot ("Why is the GPU busy?", "Why did local/default fall back?",
"What changed today?") and turning operational commands into a ProposedAction that the user must
press — the command bar never executes anything itself. Prompt text is never sent off-box: there is
no model in this path (decided_by is always "rules"); anything the rules don't recognise is handed
to Ask as a prompt, where the user sees the route before anything runs.

Rules are ordered: the first match wins, so specific phrasings ("what depends on the GPU reserve",
a knowledge question) sit above the broad ones ("…GPU…", a system question). The test table in
tests/test_console_ai.py pins every example from the spec.

Owner: AI.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

from lif.console.contracts import (CommandKind, CommandResolution, Fact, ModelRole, PromptSuggestion, ProposedAction,
                                   User)
from lif.console.poller import Snapshot

# ── rules ─────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Rule:
    name: str
    kind: CommandKind
    confidence: float
    pattern: re.Pattern[str]


def _r(name: str, kind: CommandKind, confidence: float, pattern: str) -> Rule:
    return Rule(name, kind, confidence, re.compile(pattern, re.I))


_NAV_WORDS = (r"gpu|compute|dgx|settings|logs?|jobs|models|discovery|agents|approvals|knowledge|system|services|"
              r"storage|network|home|ask|devices|connect(?: mobile)?")

RULES: tuple[Rule, ...] = (
    # Explicit hand-off to the model: "Ask the local model …", "prompt: …".
    _r("ask_prefix", "ai_prompt", 0.95, r"^(?:ask (?:the )?(?:local )?(?:model|ai|labzilla)|prompt)\b"),
    # Unambiguous creation verbs first: "Write a script that pauses batch jobs" is a prompt, not a command.
    _r("gen_strong", "ai_prompt", 0.85, r"^(?:write|generate|draft|implement|refactor|translate|convert)\b"),
    # Navigation ("open gpu", "settings", "connect a phone").
    _r("nav_connect", "navigation", 0.9,
       r"^(?:(?:open |go to |show )?connect(?: mobile)?|(?:connect|pair|add) (?:a |my |another |new )*"
       r"(?:phone|mobile|tablet|device))\b"),
    _r("nav_open", "navigation", 0.95, rf"^(?:open|go to|show(?: me)?|take me to|view)\s+(?:the\s+)?(?:{_NAV_WORDS})$"),
    _r("nav_word", "navigation", 0.9, rf"^(?:{_NAV_WORDS})$"),
    # Agents: there is no general-purpose agent runtime; say so instead of pretending (§103).
    _r("agent_scout", "agent_request", 0.9, r"\b(?:run|start|launch)\b.*\b(?:model scout|scout)\b"),
    _r("agent_evaluator", "agent_request", 0.9, r"\b(?:run|start|launch)\b.*\bevaluator\b"),
    _r("agent_run", "agent_request", 0.85, r"\b(?:run|start|launch|use|ask)\b.*\bagent\b|\bagent\b.*\b(?:on|for)\b"),
    # Model discovery ("Check for better coding models", "…better coding models were released").
    _r("discover", "model_request", 0.9,
       r"\b(?:check|look|search|scan|find|any)\b.*\b(?:better|new|newer|improved)\b.*\bmodels?\b"
       r"|\bmodel (?:discovery|upgrades?)\b|\bcheck for (?:model )?upgrades\b|\bbetter\b.*\bmodels?\b.*\breleased\b"),
    # Operational commands → ProposedAction (never executed here).
    _r("batch_pause", "operational_command", 0.95, r"\b(?:pause|stop|halt|suspend|hold)\b.*\bbatch\b"),
    _r("batch_resume", "operational_command", 0.95, r"\b(?:resume|unpause|restart|continue|un-pause)\b.*\bbatch\b"),
    _r("maint_on", "operational_command", 0.9,
       r"\b(?:enable|turn on|enter|start|switch on|activate)\b.*\bmaintenance\b|\bmaintenance(?: mode)? on\b"),
    _r("maint_off", "operational_command", 0.9,
       r"\b(?:disable|turn off|exit|leave|end|stop|switch off|deactivate)\b.*\bmaintenance\b"
       r"|\bmaintenance(?: mode)? off\b"),
    # Knowledge questions about the project's memory (§33). Above the system rules: "what depends on
    # the GPU reserve" is a question about decisions, not about the GPU right now.
    _r("kn_depends", "knowledge_query", 0.85, r"\bwhat (?:depends|relies) on\b"),
    _r("kn_why_we", "knowledge_query", 0.85, r"^why (?:are|do|did|have|were) we\b|\bwhy (?:was|were) \w+ (?:chosen|picked)\b"),
    _r("kn_incident", "knowledge_query", 0.85,
       r"\b(?:last|previous|past|earlier)\b.*\bincident\b|\bwhat happened\b.*\bincident\b"),
    _r("kn_decisions", "knowledge_query", 0.85,
       r"\bdecisions?\b.*\b(?:changed|made|this week|recently|recent|updated)\b|\bdecision records?\b"
       r"|\b(?:evidence|assumptions?)\b.*\bfor\b"),
    # System queries answered from the poller snapshot.
    _r("last_request", "system_query", 0.9,
       r"\bwhich model\b.*\b(?:handled|answered|served|used|ran)\b|\bwho (?:handled|answered)\b"
       r"|\b(?:my|the) last (?:request|answer|prompt|question)\b"),
    _r("fallback", "system_query", 0.9, r"\bfall(?:s|ing|en)?[ -]?back\b|\bfallback\b|\bdegraded\b"),
    _r("gpu", "system_query", 0.9,
       r"\b(?:gpu|dgx|blerbz|vram|unified memory|primary workload)\b|\bmemory (?:use|usage|pressure|left)\b|\bwhy is (?:it|labzilla) slow\b"),
    _r("changes", "system_query", 0.9,
       r"\bwhat(?:'s| has| have)? (?:changed|happened)\b|\bchanges? (?:today|recently)\b|\brecent activity\b"),
    _r("incidents", "system_query", 0.9,
       r"\b(?:current|open|active|ongoing)\b.*\b(?:incidents?|problems?|issues?|alerts?)\b"
       r"|\b(?:summari[sz]e|list|show)\b.*\b(?:incidents?|alerts?|problems?)\b|\b(?:anything|something) (?:wrong|broken)\b"),
    _r("approvals", "system_query", 0.85, r"\bapprov\w*\b|\bpending reviews?\b|\bneeds? (?:my )?(?:review|approval)\b"),
    _r("jobs", "system_query", 0.85,
       r"\b(?:queued|running|pending|waiting|failed)\b.*\bjobs?\b|\bjobs?\b.*\b(?:queued|running|queue|waiting)\b"
       r"|\bwhat(?:'s| is) in the queue\b"),
    _r("health", "system_query", 0.85,
       r"\bis (?:labzilla|everything|the system|local ai) (?:ok|okay|healthy|up|working|ready)\b"
       r"|^(?:status|health)$|\bsystem (?:status|health)\b"),
    # Generation verbs: a prompt for Ask (§8 "Write a Python parser for this JSON").
    _r("generate", "ai_prompt", 0.8,
       r"^(?:write|create|generate|draft|explain|summari[sz]e|translate|refactor|review|fix|debug|convert|"
       r"implement|make|give me|tell me|how (?:do|can|to|does)|what (?:is|are)|who|can you|please|help)\b"),
)

FALLBACK_KIND: CommandKind = "ai_prompt"
FALLBACK_CONFIDENCE = 0.5


def _norm(text: str) -> str:
    return " ".join(text.strip().split()).rstrip("?!.").strip()


def match(text: str) -> Rule | None:
    t = _norm(text)
    return next((r for r in RULES if r.pattern.search(t)), None)


def classify(text: str) -> tuple[CommandKind, float]:
    """Rules-first classification → (kind, confidence 0..1). Unrecognised text is a prompt for Ask."""
    r = match(text)
    return (r.kind, r.confidence) if r else (FALLBACK_KIND, FALLBACK_CONFIDENCE)


# ── shared vocabulary ─────────────────────────────────────────────────────────────────────────

NAV_TARGETS: tuple[tuple[str, str, str], ...] = (
    # (pattern, path, title)
    (r"gpu|compute|dgx", "/system/compute", "Compute"),
    (r"settings", "/system/settings", "Settings"),
    (r"logs?", "/system/logs", "Logs"),
    (r"services", "/system/services", "Services"),
    (r"storage", "/system/storage", "Storage"),
    (r"network", "/system/network", "Network"),
    (r"system", "/system", "System"),
    (r"jobs", "/jobs", "Jobs"),
    (r"discovery", "/models/discovery", "Model discovery"),
    (r"models", "/models", "Models"),
    (r"approvals", "/agents?tab=approvals", "Approvals"),
    (r"agents", "/agents", "Agents"),
    (r"knowledge", "/knowledge", "Knowledge"),
    (r"devices|connect|phone|mobile|tablet|device|pair", "/connect", "Connect a device"),
    (r"home", "/", "Home"),
    (r"ask", "/ask", "Ask"),
)

# Discovery categories the controller accepts (POST /v1/models/refresh) and the words that name them.
CATEGORY_WORDS: tuple[tuple[str, str], ...] = (
    ("coding", r"cod(?:e|ing|er)|programming"),
    ("reasoning", r"reason(?:ing)?|deep|thinking"),
    ("embedding", r"embeddings?"),
    ("reranking", r"re-?rank(?:ing|er)?"),
    ("fast", r"fast|small|quick|tiny"),
    ("general", r"general|chat|default|balanced"),
    ("vision", r"vision|image|images|multimodal|multi-modal|vlm"),
)

ROLE_WORDS: tuple[tuple[str, str], ...] = (
    ("local/default", r"default|general|balanced|primary model|main model"),
    ("local/fast", r"\bfast\b"),
    ("local/instant", r"instant"),
    ("local/reasoning", r"reasoning|deep"),
    ("local/code", r"\bcode\b|coding"),
    ("local/embedding", r"embedding"),
    ("local/vision", r"vision"),
    ("local/batch", r"\bbatch\b"),
)

NOT_READY = "Labzilla is still checking its services. Try again in a few seconds."
NO_PERM = "This session can't do this — it needs an admin session on a desktop."


def _num(v: float | None, unit: str = "", digits: int = 0) -> str:
    if v is None:
        return "unknown"
    return f"{v:,.{digits}f}{unit}"


def _ago(ts: float, now: float) -> str:
    """Relative time. The server's clock is UTC in the container, not the owner's: a wall-clock time
    or a server-midnight "today" would be off by the owner's UTC offset."""
    d = max(0.0, now - ts)
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    return f"{int(d // 3600)} h ago"


def _n(n: int, word: str, plural: str | None = None) -> str:
    """'1 batch job', '3 batch jobs': no "job(s)" in user-facing text."""
    return f"{n} {word if n == 1 else plural or word + 's'}"


def _ready(snap: Snapshot) -> bool:
    return snap.updated_at > 0


def _strip_prefix(text: str) -> str:
    t = re.sub(r"^\s*(?:ask (?:the )?(?:local )?(?:model|ai|labzilla)|prompt)\b\s*(?:to\b)?[\s:,\-–—]*", "", text,
               flags=re.I)
    return t.strip() or text.strip()


def _action(user: User, action: ProposedAction) -> tuple[ProposedAction, str | None]:
    return action, (None if action.perm in user.perms else NO_PERM)


def _with_note(answer: str, note: str | None) -> str:
    return f"{answer}\n\n{note}" if note else answer


def _settings(snap: Snapshot) -> dict:
    s = snap.raw.get("settings")
    return s if isinstance(s, dict) else {}


# ── builders ──────────────────────────────────────────────────────────────────────────────────

Builder = Callable[[str, Snapshot, User], CommandResolution]


ASK_CONFIRM = "Labzilla will pick the right local model for this. Nothing is sent until you press Ask."


def _ask(text: str, snap: Snapshot, user: User) -> CommandResolution:
    # A clear prompt carries no answer, so the client hands it straight to Ask (§13: no extra step).
    prompt = _strip_prefix(text)
    return CommandResolution(kind="ai_prompt", title="Ask local AI", prompt=PromptSuggestion(text=prompt, mode="auto"))


def _navigate(text: str, snap: Snapshot, user: User) -> CommandResolution:
    t = _norm(text).lower()
    for pat, path, title in NAV_TARGETS:
        if re.search(rf"\b(?:{pat})\b", t):
            return CommandResolution(kind="navigation", title=f"Open {title}", navigate=path)
    return CommandResolution(kind="navigation", title="Open Home", navigate="/")


def _agent(text: str, snap: Snapshot, user: User) -> CommandResolution:
    t = _norm(text).lower()
    if re.search(r"\bscout\b", t):
        action, note = _action(user, ProposedAction(
            id="agents.model_scout", label="Run Model Scout",
            description="Searches Hugging Face for models that could beat the ones Labzilla uses now.",
            impact="Uses a little network and CPU for a minute or two. Nothing is downloaded or changed.",
            confirm="none", perm="models.discover", path="/api/agents/run", body={"agent": "model-scout", "params": {}}))
        return CommandResolution(kind="agent_request", title="Run Model Scout", proposed_action=action,
                                 answer=_with_note("Model Scout checks every model category for better candidates.",
                                                   note), navigate="/agents")
    if re.search(r"\bevaluator\b", t):
        return CommandResolution(
            kind="agent_request", title="Run the Evaluator", navigate="/agents?run=1",
            answer="The Evaluator benchmarks one candidate model against the one in use. Pick the candidate on the "
                   "Agents page.")
    # "Run the code agent on repo X": no such agent is installed. Say so, and offer what exists.
    return CommandResolution(
        kind="agent_request", title="No code agent installed yet", navigate="/agents",
        prompt=PromptSuggestion(text=_strip_prefix(text), mode="code"),
        answer="Labzilla doesn't have a code agent yet. The agents installed today are **Model Scout** (looks for "
               "better models) and the **Evaluator** (benchmarks a candidate).\n\nYou can ask the local model "
               "instead: it can review code you paste or attach, but it can't open a repository on its own.",
        facts=[Fact(label="Available agents", value="Model Scout, Evaluator")])


def _categories(text: str) -> list[str]:
    t = _norm(text).lower()
    return [cat for cat, pat in CATEGORY_WORDS if re.search(rf"\b(?:{pat})\b", t)]


def _discover(text: str, snap: Snapshot, user: User) -> CommandResolution:
    cats = _categories(text)
    what = " and ".join(cats) + " models" if cats else "models in every category"
    s = _settings(snap)
    if s.get("discovery_disabled") or s.get("maintenance"):
        why = "maintenance mode is on" if s.get("maintenance") else "model discovery is switched off"
        return CommandResolution(kind="model_request", title="Model discovery is paused", navigate="/system/settings",
                                 answer=f"Labzilla can't check for better {what} right now because {why}. "
                                        "Change it in Settings, then try again.")
    action, note = _action(user, ProposedAction(
        id="models.discover", label=f"Check for better {what}",
        description="Model Scout searches Hugging Face, filters what fits this machine and shortlists candidates.",
        impact="Takes a minute or two and uses a little paid Jev screening. Nothing is downloaded, loaded or "
               "switched — candidates wait for your review.",
        confirm="none", perm="models.discover", path="/api/models/discovery", body={"categories": cats or None}))
    return CommandResolution(kind="model_request", title=f"Check for better {what}", proposed_action=action,
                             navigate="/models/discovery" + (f"?category={cats[0]}" if len(cats) == 1 else ""),
                             answer=_with_note(f"Model Scout will look for better {what}.", note))


def _resume_impact() -> str:
    from lif.gpu import workload
    if workload.enabled():
        return f"Batch work still yields to {workload.display_name()} when it needs the GPU."
    return "Queued batch work starts again."


def _batch_job_counts(snap: Snapshot) -> dict[str, int] | None:
    """Unfinished batch jobs by state from the controller's overview, or None when it isn't known."""
    ov = snap.raw.get("overview") if isinstance(snap.raw, dict) else None
    batch = ov.get("batch") if isinstance(ov, dict) else None
    jobs = batch.get("jobs") if isinstance(batch, dict) else None
    if not isinstance(jobs, dict):
        return None
    out: dict[str, int] = {}
    for k in ("running", "queued", "paused"):
        try:
            out[k] = int(jobs.get(k) or 0)
        except (TypeError, ValueError):
            out[k] = 0
    return out


def _batch(text: str, snap: Snapshot, user: User, pause: bool) -> CommandResolution:
    st = snap.status
    maintenance = bool(st.maintenance or _settings(snap).get("maintenance"))
    if _ready(snap) and st.batch_paused == pause and not (not pause and maintenance):
        state = "paused" if pause else "running normally"
        return CommandResolution(kind="operational_command", title=f"Batch jobs are already {state}",
                                 navigate="/jobs", answer=f"All batch work is already {state}. Nothing to change.")
    # Batch jobs only: Home's running count also includes downloads, benchmarks and model checks.
    counts = _batch_job_counts(snap)
    queued = sum(counts.values()) if counts is not None else st.jobs.queued + st.jobs.running + st.jobs.waiting
    if pause:
        action = ProposedAction(
            id="jobs.pause_all", label="Pause all batch jobs",
            description="Stops starting new batch items until you resume.",
            impact=(f"{_n(queued, 'batch job')} will wait. " if _ready(snap) else "") +
                   "Items already running finish normally. Interactive Ask is not affected.",
            confirm="none", perm="jobs.control", path="/api/jobs/batch/pause-all", body={"paused": True})
    else:
        action = ProposedAction(
            id="jobs.resume_all", label="Resume batch jobs",
            description="Lets queued batch work start again.",
            impact=_resume_impact() +
                   (" Maintenance mode is on, so batch stays paused until maintenance ends." if maintenance else ""),
            confirm="none", perm="jobs.control", path="/api/jobs/batch/pause-all", body={"paused": False})
    action, note = _action(user, action)
    return CommandResolution(kind="operational_command", title=action.label, proposed_action=action, navigate="/jobs",
                             answer=_with_note(action.impact, note))


def _maintenance(text: str, snap: Snapshot, user: User, on: bool) -> CommandResolution:
    if on:
        action = ProposedAction(
            id="system.maintenance_on", label="Turn on maintenance mode",
            description="Pauses Labzilla's automatic work: batch jobs, model discovery, downloads and benchmarks.",
            impact="Batch jobs stop starting new items and automation waits. Asking local AI keeps working. "
                   "Turn it off to resume.",
            # reversible and stated as such, like the Settings switch: no confirmation (§41)
            confirm="none", perm="system.settings", path="/api/system/settings",
            body={"key": "maintenance", "value": True})
    else:
        action = ProposedAction(
            id="system.maintenance_off", label="Turn off maintenance mode",
            description="Lets automatic work run again.",
            impact="Batch jobs resume (unless batch is paused separately) and automation follows its settings again.",
            confirm="none", perm="system.settings", path="/api/system/settings",
            body={"key": "maintenance", "value": False})
    action, note = _action(user, action)
    return CommandResolution(kind="operational_command", title=action.label, proposed_action=action,
                             navigate="/system/settings", answer=_with_note(action.impact, note))


def _knowledge(text: str, snap: Snapshot, user: User) -> CommandResolution:
    q = _norm(text)
    return CommandResolution(kind="knowledge_query", title="Search project knowledge",
                             navigate=f"/knowledge?q={quote(q)}",
                             answer="Searching decisions, evidence and assumptions in Labzilla's project memory.")


def _gpu(text: str, snap: Snapshot, user: User) -> CommandResolution:
    if not _ready(snap):
        return CommandResolution(kind="system_query", title="What is the GPU doing?", answer=NOT_READY,
                                 navigate="/system/compute")
    r = snap.status.resource
    util = r.gpu_util_pct
    name = r.primary_name or "Primary workload"
    # Workload states (humanize.blerbz): busy = running/reloading, reserved = GPU held,
    # imminent = likely within the hour, unknown = scheduler not visible. Shown only when enabled.
    if r.primary_enabled and r.blerbz in ("busy", "reserved"):
        head = f"**The GPU is working for {name}** — {r.blerbz_reason or r.blerbz_label}"
        tail = f"{name} has priority: local AI slows down and batch work waits until it's done. This is automatic."
    elif r.primary_enabled and r.blerbz == "unknown" and r.blerbz_reason:
        head = f"**Labzilla can't see the GPU scheduler right now** — {r.blerbz_reason}"
        tail = "" if util is None else f"GPU load is {util:.0f}%."
    elif util is not None and util >= 50:
        extra = f", and {name} is {(r.blerbz_label or 'idle').lower()}" if r.primary_enabled else ""
        head = f"**The GPU is at {util:.0f}%**{extra}."
        tail = "Most of the load comes from local AI or other work."
    elif util is not None:
        extra = f" {name} is {(r.blerbz_label or 'idle').lower()}." if r.primary_enabled else ""
        head = f"**The GPU isn't busy** — {util:.0f}% load.{extra}"
        tail = ""
    elif r.primary_enabled:
        head = f"{name} is {(r.blerbz_label or 'in an unknown state').lower()}."
        tail = "GPU load isn't measurable right now."
    else:
        head = "GPU load isn't measurable right now."
        tail = ""
    if r.primary_enabled and r.blerbz == "imminent":
        tail = (tail + " " + (r.blerbz_reason or f"{name} is likely to start soon.")).strip()
    head = head if head.endswith(".") else head + "."
    facts = [Fact(label="GPU load", value=_num(util, "%")),
             Fact(label="Unified memory", value=f"{_num(r.mem_used_gb, ' GB')} of {_num(r.mem_total_gb, ' GB')} used")]
    if r.primary_enabled:
        facts.append(Fact(label=name, value=r.blerbz_label or "unknown"))
    for w in snap.compute.work:
        if w.key in ("blerbz", "ai_serving") and w.gb is not None:
            facts.append(Fact(label=f"{w.label or w.key} memory", value=_num(w.gb, " GB", 1)))
    if snap.jobs.running or snap.jobs.waiting:
        facts.append(Fact(label="Batch jobs", value=f"{snap.jobs.running} running, {snap.jobs.waiting} waiting"))
    if r.stale:
        tail = (tail + " These readings are out of date.").strip()
    return CommandResolution(kind="system_query", title="What the GPU is doing", navigate="/system/compute",
                             answer=f"{head} {tail}".strip(), facts=facts)


def _role_for(text: str, roles: list[ModelRole]) -> ModelRole | None:
    t = _norm(text).lower()
    m = re.search(r"local/[a-z]+", t)
    if m:
        return next((r for r in roles if r.alias == m.group(0)), None)
    for alias, pat in ROLE_WORDS:
        if re.search(pat, t):
            return next((r for r in roles if r.alias == alias), None)
    return None


def _fallback(text: str, snap: Snapshot, user: User) -> CommandResolution:
    if not _ready(snap) or not snap.roles:
        return CommandResolution(kind="system_query", title="Why did local AI fall back?", answer=NOT_READY,
                                 navigate="/models")
    role = _role_for(text, snap.roles)
    if role is None:
        active = [r for r in snap.roles if r.fallback_active or r.degraded]
        if not active:
            return CommandResolution(kind="system_query", title="No fallback is active", navigate="/models",
                                     answer="Every model role is served by its primary model right now.")
        lines = [f"- **{r.label or r.alias}**: {r.cause_label or 'cause not reported'} — now served by "
                 f"{r.model_name or 'an unknown model'}" for r in active]
        return CommandResolution(kind="system_query", title="Roles running on a fallback", navigate="/models",
                                 answer="\n".join(lines))
    chain = " → ".join(role.chain) if role.chain else "not reported"
    facts = [Fact(label="Serving now", value=role.model_name or "nothing"),
             Fact(label="Primary model", value=role.chain[0] if role.chain else "none deployed"),
             Fact(label="Fallback order", value=chain)]
    if role.fallback_active or role.degraded:
        what = "is running on its fallback" if role.fallback_active else "is running on a smaller model than it expects"
        answer = f"**{role.label or role.alias} ({role.alias}) {what}** — {role.cause_label or 'the cause was not reported'}."
        if role.model_name:
            answer += f" Requests are answered by {role.model_name}."
        if role.cause in ("yielded_to_primary", "shed_by_memory_guard"):
            answer += " This is automatic and reverses itself when that capacity is free."
    elif role.state in ("offline", "attention"):
        answer = f"**{role.label or role.alias} ({role.alias}) is unavailable** — {role.cause_label or role.state_label}."
    else:
        answer = f"{role.label or role.alias} ({role.alias}) isn't falling back right now: {role.model_name or 'its primary'} is serving it."
    return CommandResolution(kind="system_query", title=f"Why {role.alias} fell back", answer=answer, facts=facts,
                             navigate=f"/models/roles/{role.role}")


def _changes(text: str, snap: Snapshot, user: User) -> CommandResolution:
    if not _ready(snap):
        return CommandResolution(kind="system_query", title="What changed today", answer=NOT_READY,
                                 navigate="/system/logs")
    now = time.time()
    today = [e for e in snap.activity if e.ts >= now - 86400]     # "today" = the last 24 hours (see _ago)
    if not today:
        return CommandResolution(kind="system_query", title="What changed today", navigate="/system/logs",
                                 answer="Nothing notable has happened in the last 24 hours.")
    lines = [f"- {_ago(e.ts, now)}: {e.title}" for e in today[:8]]
    more = f"\n\n…and {len(today) - 8} more in Logs." if len(today) > 8 else ""
    return CommandResolution(kind="system_query", title="What changed today", navigate="/system/logs",
                             answer="\n".join(lines) + more,
                             facts=[Fact(label="Events in the last 24 hours", value=str(len(today)))])


def _incidents(text: str, snap: Snapshot, user: User) -> CommandResolution:
    if not _ready(snap):
        return CommandResolution(kind="system_query", title="Current incidents", answer=NOT_READY,
                                 navigate="/system/services")
    lines: list[str] = []
    for s in snap.services:
        if s.health not in ("healthy", "unknown", "busy"):
            lines.append(f"- **{s.name or s.key}**: {s.summary}" + (f" — {s.impact}" if s.impact else ""))
    seen = {line.lower() for line in lines}
    for n in snap.status.notifications:
        if n.severity in ("warning", "error"):
            line = f"- **{n.title}**: {n.body}" if n.body else f"- **{n.title}**"
            if line.lower() not in seen:
                lines.append(line)
    for r in snap.roles:
        if r.fallback_active and not any(r.alias in ln for ln in lines):
            lines.append(f"- **{r.label or r.alias}** is on its fallback: {r.cause_label or 'cause not reported'}")
    if not lines:
        return CommandResolution(kind="system_query", title="No current incidents", navigate="/system/services",
                                 answer=f"Nothing needs attention. {snap.status.headline or ''}".strip())
    return CommandResolution(kind="system_query", title=f"{_n(len(lines), 'current issue')}", navigate="/system/services",
                             answer="\n".join(lines[:10]))


def _approvals(text: str, snap: Snapshot, user: User) -> CommandResolution:
    n = len(snap.approvals) or snap.status.approvals_pending
    if not n:
        return CommandResolution(kind="system_query", title="Nothing waiting for approval",
                                 navigate="/agents?tab=approvals", answer="No agent or device is waiting on you.")
    lines = [f"- {a.title}" + ("" if a.blocking else " (nothing is blocked on it)") for a in snap.approvals[:6]]
    return CommandResolution(kind="system_query", title=f"{n} waiting for you", navigate="/agents?tab=approvals",
                             answer="\n".join(lines) or f"{_n(n, 'item')} {'waits' if n == 1 else 'wait'} for a decision.")


def _jobs(text: str, snap: Snapshot, user: User) -> CommandResolution:
    if not _ready(snap):
        return CommandResolution(kind="system_query", title="Jobs", answer=NOT_READY, navigate="/jobs")
    j = snap.jobs
    if not (j.running or j.queued or j.waiting):
        answer = "No background jobs are running or queued."
    else:
        answer = f"{j.running} running, {j.queued} queued and {j.waiting} waiting."
        name = snap.status.resource.primary_name or "the primary workload"
        if j.waiting and snap.status.resource.primary_enabled and snap.status.resource.blerbz in ("busy", "reserved", "unknown"):
            answer += f" Waiting jobs resume automatically when {name} is done with the GPU."
        elif snap.status.batch_paused:
            answer += " Batch work is paused."
    facts = [Fact(label="Running", value=str(j.running)), Fact(label="Queued", value=str(j.queued)),
             Fact(label="Waiting", value=str(j.waiting)), Fact(label="Failed (24 h)", value=str(j.failed_24h))]
    return CommandResolution(kind="system_query", title="Background jobs", navigate="/jobs", answer=answer, facts=facts)


def _health(text: str, snap: Snapshot, user: User) -> CommandResolution:
    if not _ready(snap):
        return CommandResolution(kind="system_query", title="Labzilla status", answer=NOT_READY, navigate="/")
    st = snap.status
    facts = [Fact(label="Local AI", value=st.local_ai_label or st.local_ai),
             Fact(label="Primary model", value=st.primary_model or "unknown")]
    if st.resource.primary_enabled:
        facts.append(Fact(label=st.resource.primary_name or "Primary workload", value=st.resource.blerbz_label or "unknown"))
    return CommandResolution(kind="system_query", title=st.headline or "Labzilla status", navigate="/",
                             answer=st.headline or "Status unknown.", facts=facts)


def _last_request(text: str, snap: Snapshot, user: User) -> CommandResolution:
    from lif.console import threads   # sqlite read; imported here so classify() stays dependency-free

    try:
        hit = threads.latest_receipt(user.id)
    except Exception:                  # database unavailable: answer honestly rather than fail the bar
        hit = None
    if hit is None:
        return CommandResolution(kind="system_query", title="No requests yet", navigate="/ask",
                                 answer="You haven't asked anything from the console yet, so there's no model to report.")
    thread_id, title, msg = hit
    r = msg.receipt
    assert r is not None
    who = r.model_name or r.served_by or "an unknown model"
    secs = f" in {r.latency_ms / 1000:.1f} s" if r.latency_ms else ""
    answer = f"Your last request (“{title}”) was handled by **{r.role_label or r.alias}** — {who}{secs}."
    if r.fallback and r.reason_label:
        answer += f" It used a fallback: {r.reason_label}."
    facts = [Fact(label="Role", value=f"{r.role_label} ({r.alias})" if r.role_label else r.alias),
             Fact(label="Model", value=who),
             Fact(label="Route", value=" → ".join(s.label for s in r.route) or "—"),
             Fact(label="Privacy", value={"local_only": "Local only", "local_jev": "Local + Jev",
                                          "external": "External model used"}[r.privacy])]
    return CommandResolution(kind="system_query", title="Which model handled your last request",
                             navigate=f"/ask/{thread_id}", answer=answer, facts=facts)


BUILDERS: dict[str, Builder] = {
    "ask_prefix": _ask, "gen_strong": _ask, "generate": _ask,
    "nav_connect": _navigate, "nav_open": _navigate, "nav_word": _navigate,
    "agent_scout": _agent, "agent_evaluator": _agent, "agent_run": _agent,
    "discover": _discover,
    "batch_pause": lambda t, s, u: _batch(t, s, u, True),
    "batch_resume": lambda t, s, u: _batch(t, s, u, False),
    "maint_on": lambda t, s, u: _maintenance(t, s, u, True),
    "maint_off": lambda t, s, u: _maintenance(t, s, u, False),
    "kn_depends": _knowledge, "kn_why_we": _knowledge, "kn_incident": _knowledge, "kn_decisions": _knowledge,
    "last_request": _last_request, "fallback": _fallback, "gpu": _gpu, "changes": _changes,
    "incidents": _incidents, "approvals": _approvals, "jobs": _jobs, "health": _health,
}


def resolve(text: str, snap: Snapshot, user: User) -> CommandResolution:
    """Classify and build the full resolution (answer, facts, proposed action, navigation or prompt hand-off)."""
    rule = match(text)
    if rule is None:
        # Nothing recognised it: it is probably a prompt, but confirm before sending (it may be a command we missed).
        out = _ask(text, snap, user)
        out.confidence = FALLBACK_CONFIDENCE
        out.answer = ASK_CONFIRM
        return out
    out = BUILDERS[rule.name](text, snap, user)
    out.kind, out.confidence, out.decided_by = rule.kind, rule.confidence, "rules"
    return out
