"""Representation router (spec §8–§12, §46, §87, §91–§92, §107–§110).

The question is "what is the cheapest representation that communicates this well", not "what is the
fanciest artifact we can make". For each renderer in the registry:

    Utility(R) = fit·(0.55 + 0.35·clarity)                     relationship fit × how directly R shows it
               + 0.20·interactivity + 0.15·learning_value     only when parameters change outcomes;
                                                              interactivity × min(1, budget ÷ time to use it)
               + audience_fit + viewport_fit
               − 0.25·latency − 0.60·overload − cost − 0.30·unsupported
    fit          max strength of the structures R is best_for (analysis.Features.structure)
    latency      expected render seconds ÷ time budget        overload: (time to take it in ÷ budget − 0.5) ÷ 2, ≤ 1.5
    cost         resource class (LIGHT 0 … LONG_RUNNING 0.4)  unsupported: strongest structure R cannot show

The best renderer is PRIMARY. Others are added only while their marginal value clears THRESHOLD:

    AdditionalValue(R) = 0.8·new_structure_covered + 0.2·Utility(R) − overload − latency − cost

and only up to the artifact cap for the time budget. A short STE summary always comes first unless
prose is primary (fast first response, and the text alternative for every visual).

GPU and long-running renderers are deferred, never dropped silently, while the primary workload is
busy. Jev judgments (bounded, over derived features only) may adjust scores; code
scores stand alone when Jev is unavailable or its answer is not actionable.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

from lif.understanding.analysis import Features
from lif.understanding.registry import Registry
from lif.understanding.render.base import LATENCY_SECONDS, RendererCapabilities
from lif.understanding.spec import DEPTH_SECONDS, Audience, ExplanationSpec

THRESHOLD = 0.30
COST = {"LIGHT": 0.0, "CPU": 0.02, "BROWSER": 0.05, "GPU": 0.25, "LONG_RUNNING": 0.40}
PROSE = ("STE_PROSE", "STRUCTURED_PROSE")
# User words → render targets (§92). "text" keeps prose only.
FORMAT_ALIASES = {
    "text": "STE_PROSE", "prose": "STE_PROSE", "ste": "STE_PROSE", "markdown": "STRUCTURED_PROSE",
    "structured": "STRUCTURED_PROSE", "diagram": "MERMAID", "mermaid": "MERMAID", "sketch": "EXCALIDRAW",
    "excalidraw": "EXCALIDRAW", "table": "TABLE", "html": "STATIC_VISUAL_EXPLAINER",
    "explainer": "STATIC_VISUAL_EXPLAINER", "interactive": "INTERACTIVE_HTML", "steps": "INTERACTIVE_HTML",
    "simulation": "SIMULATION", "simulate": "SIMULATION", "video": "NARRATED_VIDEO", "animation": "ANIMATION",
    "canvas": "INTERACTIVE_CANVAS", "app": "MINI_APPLICATION", "timeline": "MERMAID",
}


@dataclass
class ResourceState:
    """What the GPU is doing for the primary workload (lif.gpu.state.BlerbzState names)."""
    blerbz_state: Literal["LOW", "MODERATE", "HIGH", "IMMINENT", "UNKNOWN"] = "UNKNOWN"
    override: bool = False                   # operator said explanation work may run anyway

    @property
    def primary_busy(self) -> bool:
        return not self.override and self.blerbz_state in ("HIGH", "IMMINENT", "UNKNOWN")


@dataclass
class RouteContext:
    depth: str = "learn"
    time_budget_seconds: int | None = None
    audience: Audience | None = None
    viewport: Literal["desktop", "mobile"] = "desktop"
    interaction_allowed: bool = True
    local_only: bool = True
    format: str = "auto"                     # auto | a FORMAT_ALIASES key | a render target
    resources: ResourceState = field(default_factory=ResourceState)
    judgments: dict[str, str] = field(default_factory=dict)   # actionable Jev answers by decision name

    @property
    def budget(self) -> int:
        return int(self.time_budget_seconds or DEPTH_SECONDS.get(self.depth, 180))


@dataclass
class Score:
    renderer: str
    target: str
    utility: float
    terms: dict[str, float]
    available: bool
    reason: str = ""


@dataclass
class RoutePlan:
    primary: str
    supporting: list[str]
    deferred: list[dict]
    excluded: list[dict]
    scores: list[Score]
    why: list[str]
    options: dict[str, str] = field(default_factory=dict)
    judgments: dict[str, str] = field(default_factory=dict)

    @property
    def selected(self) -> list[str]:
        return [self.primary, *self.supporting]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["scores"] = [asdict(s) for s in sorted(self.scores, key=lambda s: -s.utility)]
        return d


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def _cap(budget: int) -> int:
    return 2 if budget <= 45 else 3 if budget <= 240 else 4


def score(caps: RendererCapabilities, f: Features, ctx: RouteContext) -> tuple[float, dict[str, float]]:
    st, budget = f.structure, ctx.budget
    who = ctx.audience or Audience()
    fit = max((st.get(k, 0.0) for k in caps.best_for), default=0.0)
    unsupported = max((st.get(k, 0.0) for k in caps.unsupported), default=0.0)
    interactivity = st.get("parameter", 0.0) if caps.interactive and "parameter" in caps.best_for else \
        (0.3 * max(st.get("process", 0), st.get("causal", 0)) if caps.interactive and ctx.viewport == "mobile" else 0.0)
    interactivity *= _clip(budget / max(caps.consume_seconds, 1))     # no time to play → little value in controls
    if caps.interactive and not ctx.interaction_allowed:
        interactivity = -1.0
    learning = st.get("parameter", 0.0) if "parameter" in caps.best_for and ctx.depth in ("learn", "deep") else 0.0
    aud = 0.0
    if who.role == "executive":
        aud += -0.15 if caps.interactive else 0.0
        aud += 0.08 if caps.target in PROSE else 0.0
        aud += -0.10 if "quantitative" in caps.best_for and caps.target != "SIMULATION" else 0.0
    if who.expertise == "novice":
        aud += 0.05 if caps.target in (*PROSE, "MERMAID", "INTERACTIVE_HTML") else 0.0
        aud += -0.05 if caps.target == "SIMULATION" else 0.0
    if who.expertise == "expert" or who.role in ("researcher", "domain_expert"):
        aud += 0.05 if caps.target in ("STATIC_VISUAL_EXPLAINER", "TABLE", "SIMULATION") else 0.0
    view = 0.0
    if ctx.viewport == "mobile":
        view -= 0.30 if not caps.mobile else 0.0
        view -= 0.10 if caps.consume_seconds > 120 else 0.0
        view += 0.10 if caps.target == "INTERACTIVE_HTML" else 0.0    # step-through instead of a wide diagram
    latency = _clip(LATENCY_SECONDS[caps.latency_class] / max(budget, 1))
    overload = min(1.5, max(0.0, (caps.consume_seconds / max(budget, 1) - 0.5) / 2))   # grows with how far over budget
    cost = COST[caps.resource_class]
    u = (fit * (0.55 + 0.35 * caps.clarity) + 0.20 * interactivity + 0.15 * learning + aud + view
         - 0.25 * latency - 0.60 * overload - cost - 0.30 * unsupported)
    # Jev adjustments (bounded judgments over features; only actionable answers reach here).
    j = ctx.judgments
    if j.get("understanding-prose-sufficient") == "yes" and caps.target not in PROSE:
        u *= 0.75
    if j.get("understanding-interaction-worthwhile") == "no" and caps.interactive:
        u *= 0.6
    terms = {"fit": round(fit, 3), "clarity": caps.clarity, "interactivity": round(interactivity, 3),
             "learning": round(learning, 3), "audience": round(aud, 3), "viewport": round(view, 3),
             "latency": round(latency, 3), "overload": round(overload, 3), "cost": cost,
             "unsupported": round(unsupported, 3)}
    return round(u, 4), terms


def _describe(f: Features, caps: RendererCapabilities) -> str:
    st = f.structure
    bits = []
    if st.get("causal", 0) >= 0.25 and "causal" in caps.best_for:
        bits.append(f"{f.causal_edges} causal links")
    if st.get("process", 0) >= 0.2 and "process" in caps.best_for:
        bits.append(f"a {f.process_steps}-step sequence")
    if st.get("temporal", 0) >= 0.25 and "temporal" in caps.best_for:
        bits.append(f"{f.timeline_events} timed events")
    if st.get("comparison", 0) >= 0.25 and "comparison" in caps.best_for:
        bits.append(f"a comparison with {f.comparison_cells} cells")
    if st.get("parameter", 0) > 0 and "parameter" in caps.best_for:
        bits.append(f"outcomes that change with {f.variable_count} parameters")
    if st.get("definition", 0) >= 0.5 and "definition" in caps.best_for:
        bits.append(f"a definition with {f.concept_count} concepts and few relationships")
    if not bits and caps.target in PROSE:
        return "little structure to draw: a short answer says it best"
    return " and ".join(bits) or "the best overall fit"


NAMES = {"STE_PROSE": "Summary", "STRUCTURED_PROSE": "Structured text", "MERMAID": "Diagram",
         "EXCALIDRAW": "Sketch", "TABLE": "Table", "STATIC_VISUAL_EXPLAINER": "Visual explainer",
         "INTERACTIVE_HTML": "Step-through", "SIMULATION": "Simulation", "ANIMATION": "Animation",
         "NARRATED_VIDEO": "Video", "INTERACTIVE_CANVAS": "Canvas", "MINI_APPLICATION": "Mini-app"}


def route(spec: ExplanationSpec, f: Features, registry: Registry, ctx: RouteContext) -> RoutePlan:
    scores: list[Score] = []
    excluded: list[dict] = []
    deferred: list[dict] = []
    usable: dict[str, Score] = {}
    caps_of: dict[str, RendererCapabilities] = {}
    for r in registry.all():
        caps = r.capabilities()
        caps_of[caps.name] = caps
        u, terms = score(caps, f, ctx)
        why_not = caps.unavailable_reason if not caps.available else r.accepts(spec)
        if not why_not and ctx.local_only and not caps.local:
            why_not = "needs a non-local service and this request is local-only"
        if not why_not and caps.interactive and not ctx.interaction_allowed:
            why_not = "interaction is not allowed for this request"
        s = Score(caps.name, caps.target, u, terms, available=not why_not, reason=why_not or "")
        scores.append(s)
        if why_not:
            excluded.append({"renderer": caps.name, "target": caps.target, "reason": why_not, "utility": u})
        else:
            usable[caps.name] = s

    def blocked(name: str) -> str:
        c = caps_of[name]
        if c.resource_class in ("GPU", "LONG_RUNNING") and ctx.resources.primary_busy:
            return f"deferred: the primary workload has GPU priority (state {ctx.resources.blerbz_state})"
        return ""

    ranked = sorted(usable.values(), key=lambda s: (-s.utility, s.renderer))
    why: list[str] = []
    options: dict[str, str] = {}
    fam = ctx.judgments.get("understanding-diagram-family")
    if fam in ("causal", "process", "dependency", "timeline"):
        options["diagram_family"] = fam
    wanted = FORMAT_ALIASES.get(ctx.format.lower(), ctx.format.upper()) if ctx.format not in ("", "auto") else ""
    if wanted:
        pick = next((s for s in ranked if s.target == wanted), None)
        if pick is not None and not blocked(pick.renderer):
            primary = pick
            why.append(f"{NAMES.get(wanted, wanted)} selected because you asked for it.")
        else:
            reason = blocked(pick.renderer) if pick else next(
                (e["reason"] for e in excluded if e["target"] == wanted), "no renderer for that format")
            if pick is not None:
                deferred.append({"renderer": pick.renderer, "target": wanted, "reason": reason})
            fallback = [s for s in ranked if not blocked(s.renderer)]
            primary = fallback[0]
            why.append(f"{NAMES.get(wanted, wanted)} not available now ({reason}); "
                       f"showing {NAMES.get(primary.target, primary.target).lower()} instead.")
        if wanted in PROSE:
            return RoutePlan(primary.renderer, [], deferred, excluded, scores, why, options, dict(ctx.judgments))
    else:
        candidates = [s for s in ranked if not blocked(s.renderer)]
        for s in ranked:
            if blocked(s.renderer) and s.utility >= THRESHOLD:
                deferred.append({"renderer": s.renderer, "target": s.target, "reason": blocked(s.renderer)})
        primary = candidates[0]
        why.append(f"{NAMES.get(primary.target, primary.target)} selected because the explanation has "
                   f"{_describe(f, caps_of[primary.renderer])}.")

    supporting: list[str] = []
    if primary.target not in PROSE:
        ste = next((s for s in ranked if s.target == "STE_PROSE"), None)
        if ste is not None:
            supporting.append(ste.renderer)
    covered = {k: (f.structure.get(k, 0.0) if k in caps_of[primary.renderer].best_for else 0.0)
               for k in f.structure}
    cap = _cap(ctx.budget)
    for s in ranked:
        if len(supporting) + 1 >= cap or s.renderer == primary.renderer or s.renderer in supporting:
            continue
        if s.target in PROSE:
            continue
        c = caps_of[s.renderer]
        gain = sum(max(0.0, f.structure.get(k, 0.0) - covered.get(k, 0.0)) for k in c.best_for)
        t = s.terms
        value = 0.8 * _clip(gain) + 0.2 * s.utility - t["overload"] - t["latency"] - t["cost"]
        if value >= THRESHOLD and s.utility >= THRESHOLD:
            if blocked(s.renderer):
                if not any(d["renderer"] == s.renderer for d in deferred):
                    deferred.append({"renderer": s.renderer, "target": s.target, "reason": blocked(s.renderer)})
                continue
            supporting.append(s.renderer)
            for k in c.best_for:
                covered[k] = max(covered.get(k, 0.0), f.structure.get(k, 0.0))
            why.append(f"{NAMES.get(s.target, s.target)} added: it shows {_describe(f, c)} "
                       f"(marginal value {value:.2f}).")
    # One line on the most tempting renderer that was not made (§91).
    for s in sorted(scores, key=lambda s: -s.utility):
        if s.renderer in (primary.renderer, *supporting):
            continue
        name = NAMES.get(s.target, s.target)
        if len(why) >= 3 and s.target not in ("NARRATED_VIDEO", "ANIMATION"):
            continue                     # keep "why not video" even when the list is full
        if not s.available:
            if s.target in ("NARRATED_VIDEO", "ANIMATION"):
                why.append(f"{name} not generated: {s.reason}.")
        elif any(d["renderer"] == s.renderer for d in deferred):
            why.append(f"{name} {next(d['reason'] for d in deferred if d['renderer'] == s.renderer)}.")
        elif s.utility >= THRESHOLD:
            why.append(f"{name} not generated: it adds little beyond what is already shown.")
    return RoutePlan(primary.renderer, supporting, deferred, excluded, scores, why, options, dict(ctx.judgments))
