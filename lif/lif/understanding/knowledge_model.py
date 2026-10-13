"""Knowledge model builders and the explanation planner (spec §22, §24, §76).

    question + evidence/state ──builder──▶ KnowledgeModel ──planner──▶ ExplanationSpec

Builders, cheapest first:
  package   a reviewed spec from explanation-packages/ matches the question (no model, offline)
  state     deterministic rules over collected system state (the dogfood "why is GPU utilization low")
  llm       a local model drafts the KnowledgeModel as JSON; it is validated and repaired once, and
            never sent off-box (data_class CONFIDENTIAL, allow_external False)

The planner is code: it picks the headline, assigns depth levels, writes learning objectives and
checkpoints from the structure, and makes sure every uncertainty is visible wherever its claim is.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from lif.understanding import packages
from lif.understanding.spec import (Analogy, Audience, CausalChain, Checkpoint, Claim, Comparison, Concept, Constraint,
                                    Equation, Evidence, Example, ExplanationSpec, Metadata, Misconception, Objective,
                                    Process, Relationship, SimulationSpec, SourceRef, Summary, Term, Timeline,
                                    Uncertainty, Variable)
from lif.understanding.validate import validate


class KnowledgeModel(BaseModel):
    """What is known, before any decision about presentation."""
    model_config = ConfigDict(extra="forbid")
    question: str
    headline: str                                # claim id that answers the question
    terms: list[Term] = []
    concepts: list[Concept] = []
    claims: list[Claim] = []
    evidence: list[Evidence] = []
    relationships: list[Relationship] = []
    causal_chains: list[CausalChain] = []
    processes: list[Process] = []
    timelines: list[Timeline] = []
    comparisons: list[Comparison] = []
    variables: list[Variable] = []
    equations: list[Equation] = []
    examples: list[Example] = []
    analogies: list[Analogy] = []
    constraints: list[Constraint] = []
    uncertainties: list[Uncertainty] = []
    misconceptions: list[Misconception] = []
    source_refs: list[SourceRef] = []
    simulation: SimulationSpec | None = None
    builder: str = ""
    model: str = ""
    data_class: str = "CONFIDENTIAL"
    source_snapshot: str = ""


# ── planner ──────────────────────────────────────────────────────────────────────────────────

def plan(km: KnowledgeModel, audience: Audience | None = None, spec_id: str | None = None,
         parent: str = "") -> ExplanationSpec:
    claims = [c.model_copy() for c in km.claims]
    by_id = {c.id: c for c in claims}
    head = by_id[km.headline]
    head.level, head.importance = 0, "primary"
    for c in claims:
        if c.id != head.id and "level" not in c.model_fields_set:
            c.level = {"primary": 1, "supporting": 1, "detail": 3}[c.importance]
    summary = [head.id] + [c.id for c in claims if c.importance == "primary" and c.id != head.id][:3]

    # Every element a visible claim depends on must be visible at the same depth.
    concepts = [c.model_copy() for c in km.concepts]
    cby = {c.id: c for c in concepts}
    for c in claims:
        for cid in c.concepts:
            if cid in cby:
                cby[cid].level = min(cby[cid].level, c.level)
    chains = sorted((ch.model_copy() for ch in km.causal_chains), key=lambda ch: ch.claim != head.id)
    for ch in chains:
        if ch.claim == head.id:
            ch.level = 0
        elif "level" not in ch.model_fields_set:
            ch.level = 2
        for s in ch.steps:
            if s in cby:
                cby[s].level = min(cby[s].level, ch.level)
    uncs = [u.model_copy() for u in km.uncertainties]
    for u in uncs:
        if u.about in by_id:
            u.level = min(u.level, by_id[u.about].level)
            by_id[u.about].confidence = u.confidence

    objectives: list[Objective] = [Objective(id="obj-answer", level=0, text=f"Answer: {km.question.rstrip('?')}",
                                             claims=[head.id])]
    supporting = [c.id for c in claims if c.importance == "supporting" and c.level <= 2]
    if supporting:
        objectives.append(Objective(id="obj-factors", level=2, text="Name the factors that contribute",
                                    claims=supporting[:4]))
    checkpoints: list[Checkpoint] = []
    for ch in chains[:1]:
        last = cby.get(ch.steps[-1])
        if last is not None and ch.claim:
            checkpoints.append(Checkpoint(id="cp-chain", level=2, question=f"What leads to {last.label.lower()}?",
                                          answer_claims=[ch.claim], objective="obj-answer"))
    return ExplanationSpec(
        id=spec_id or f"x-{uuid.uuid4().hex[:12]}", question=km.question, audience=audience or Audience(),
        terms=km.terms, learning_objectives=objectives, summary=Summary(headline=head.id, claims=summary),
        concepts=concepts, claims=claims, evidence=km.evidence, relationships=km.relationships, causal_chains=chains,
        processes=km.processes, timelines=km.timelines, comparisons=km.comparisons, variables=km.variables,
        equations=km.equations, examples=km.examples, analogies=km.analogies, constraints=km.constraints,
        uncertainties=uncs, misconceptions=km.misconceptions, checkpoints=checkpoints, source_refs=km.source_refs,
        simulation=km.simulation,
        metadata=Metadata(builder=km.builder, model=km.model, data_class=km.data_class, parent=parent,
                          source_snapshot=km.source_snapshot))


# ── builders ─────────────────────────────────────────────────────────────────────────────────

def from_package(question: str) -> ExplanationSpec | None:
    e = packages.find(question)
    if e is None:
        return None
    spec = e.spec.model_copy(deep=True)
    spec.id = f"x-{uuid.uuid4().hex[:12]}"
    spec.question = question
    spec.metadata = spec.metadata.model_copy(update={"package": e.ref, "created_at": time.time()})
    return spec


GIB = 1024.0
MEM_FLOOR_MIB = 8 * 1024          # below this the memory guard holds CPU-tier work back


def from_gpu_state(question: str, st: dict[str, Any]) -> KnowledgeModel:
    """Deterministic explanation of GPU utilization from collected state (collect.gpu_state)."""
    gpu, bl, pods = st.get("gpu") or {}, st.get("blerbz") or {}, st.get("pods") or []
    util = gpu.get("util_percent")
    mem_avail = gpu.get("mem_available_mib")
    pending = [p for p in pods if p.get("phase") == "Pending" and p.get("gpu")
               and (p.get("unschedulable") or p.get("reason") == "Unschedulable")]
    running = [p for p in pods if p.get("phase") == "Running" and p.get("gpu")]
    leases_p, leases_b = int(bl.get("production_leases") or 0), int(bl.get("background_leases") or 0)
    state = str(bl.get("state") or "UNKNOWN")
    residents = st.get("residents") or {}
    ts = st.get("collected_at") or time.time()

    concepts = [Concept(id="gpu", level=0, label="GPU", kind="entity", definition="The DGX Spark's single GPU."),
                Concept(id="gpu-idle", level=0, label="GPU mostly idle", kind="state"),
                Concept(id="low-util", level=0, label="Low GPU utilization", kind="state"),
                Concept(id="gpusched", level=1, label="gpusched", kind="entity",
                        definition="The host's GPU admission authority; every GPU user takes a lease from it.")]
    srcs = [SourceRef(id="src-gpusched", kind="metric", ref="gpusched /metrics", title="gpusched metrics",
                      observed_at=ts),
            SourceRef(id="src-util", kind="metric", ref=str(gpu.get("util_source") or "gpusched_gpu_util_percent"),
                      title="GPU utilization", observed_at=ts),
            SourceRef(id="src-pods", kind="k8s", ref="pods", title="Kubernetes pods", observed_at=ts),
            SourceRef(id="src-meminfo", kind="state", ref="/proc/meminfo:MemAvailable", title="Host memory",
                      observed_at=ts)]
    ev, claims, chains, rels, uncs = [], [], [], [], []
    if util is not None:
        ev.append(Evidence(id="e-util", level=1, source="src-util", value=float(util), unit="%",
                           text=f"GPU utilization was {util:g}% when sampled."))
        claims.append(Claim(id="c-util", level=1, kind="observed", importance="primary", concepts=["gpu", "low-util"],
                            evidence=["e-util"], text=f"GPU utilization was {util:g}% when sampled."))
    known = bool(bl)                          # gpusched answered: leases and state are facts, not guesses
    errors = st.get("errors") or {}
    concepts.append(Concept(id="leases", level=1, label="GPU leases", kind="quantity",
                            definition="Grants from gpusched to use the GPU."))
    if known:
        ev.append(Evidence(id="e-leases", level=1, source="src-gpusched", value=float(leases_p + leases_b),
                           text=f"gpusched reports {leases_p} production and {leases_b} background GPU leases."))
        claims.append(Claim(id="c-leases", level=1, kind="observed", importance="supporting",
                            concepts=["leases", "gpusched"], evidence=["e-leases"],
                            text=f"gpusched reports {leases_p} production and {leases_b} background GPU leases."))
    causes: list[tuple[float, str]] = []      # (strength, claim id)
    if pending:
        msg = (pending[0].get("message") or pending[0].get("reason") or "").strip()[:160]
        ev.append(Evidence(id="e-pending", level=1, source="src-pods", value=float(len(pending)),
                           text=f"{len(pending)} GPU pod(s) are Pending" + (f": {msg}" if msg else ".")))
        concepts += [Concept(id="pending-pods", level=0, label="GPU pods pending", kind="state"),
                     Concept(id="constraints", level=0, label="Scheduling constraints", kind="idea",
                             definition="Rules a node must meet before Kubernetes places a pod on it.")]
        claims.append(Claim(id="c-pending", level=1, kind="observed", importance="primary",
                            concepts=["pending-pods"], evidence=["e-pending"],
                            text=f"{len(pending)} GPU pod(s) are Pending" + (f": {msg}" if msg else ".")))
        claims.append(Claim(id="c-cause-sched", kind="inferred", importance="primary",
                            concepts=["constraints", "pending-pods", "gpu-idle"], evidence=["e-pending"],
                            text="Pods that want the GPU cannot be scheduled, so the GPU stays idle while work waits."))
        chains.append(CausalChain(id="chain-sched", label="Why the GPU is idle", claim="c-cause-sched",
                                  steps=["constraints", "pending-pods", "gpu-idle", "low-util"]))
        causes.append((0.9, "c-cause-sched"))
    busy = known and state in ("HIGH", "IMMINENT")
    concepts.append(Concept(id="primary", level=0, label="Primary workload", kind="entity",
                            definition="The production media workload; it has GPU priority over Labzilla."))
    if known:
        ev.append(Evidence(id="e-state", level=1, source="src-gpusched",
                           text=f"Primary-workload state is {state} ({bl.get('reason') or 'no reason given'})."))
        claims.append(Claim(id="c-state", level=1, kind="observed", importance="supporting",
                            concepts=["primary", "gpusched"], evidence=["e-state"],
                            text=f"Primary-workload state is {state} ({bl.get('reason') or 'no reason given'})."))
    if busy and not leases_p:
        concepts.append(Concept(id="holdback", level=0, label="Labzilla holds back", kind="event",
                                definition="Background GPU work waits while the primary workload may need the GPU."))
        claims.append(Claim(id="c-cause-hold", kind="inferred", importance="primary",
                            concepts=["holdback", "primary", "gpu-idle"], evidence=["e-state"],
                            text="Labzilla keeps background GPU work back to protect the primary workload, so the GPU "
                                 "waits for production work that has not started yet."))
        chains.append(CausalChain(id="chain-hold", label="Why the GPU is idle", claim="c-cause-hold",
                                  steps=["primary", "holdback", "gpu-idle", "low-util"]))
        causes.append((0.8, "c-cause-hold"))
    if mem_avail is not None:
        ev.append(Evidence(id="e-mem", level=1, source="src-meminfo", value=round(mem_avail / GIB, 1), unit="GiB",
                           text=f"Host MemAvailable was {mem_avail / GIB:.1f} GiB."))
        if mem_avail < MEM_FLOOR_MIB:
            concepts.append(Concept(id="memory-guard", level=0, label="Memory guard", kind="entity",
                                    definition="Labzilla component that stops starting work when free memory is low."))
            claims.append(Claim(id="c-cause-mem", kind="inferred", importance="primary",
                                concepts=["memory-guard", "gpu-idle"], evidence=["e-mem"],
                                text=f"Free memory is {mem_avail / GIB:.1f} GiB, below the 8 GiB floor, "
                                     "so new GPU work is held back until memory frees up."))
            chains.append(CausalChain(id="chain-mem", label="Why the GPU is idle", claim="c-cause-mem",
                                      steps=["memory-guard", "gpu-idle", "low-util"]))
            causes.append((0.7, "c-cause-mem"))
    loaded = sorted(n for n, ok in residents.items() if ok)
    if loaded:
        ev.append(Evidence(id="e-res", level=2, source="src-gpusched", value=float(len(loaded)),
                           text=f"{len(loaded)} resident model(s) are loaded in GPU memory."))
        concepts.append(Concept(id="residents", level=1, label="Resident models", kind="entity",
                                definition="Models kept loaded in memory so they answer quickly."))
        claims.append(Claim(id="c-res", level=2, kind="observed", importance="supporting",
                            concepts=["residents", "gpu"],
                            evidence=["e-res"],
                            text=f"{len(loaded)} resident model(s) are loaded in GPU memory."))
        concepts.append(Concept(id="memory-vs-compute", level=2, label="Memory is not compute", kind="idea",
                                definition="Loaded models use GPU memory; utilization counts only active compute."))
        claims.append(Claim(id="c-res-idle", level=2, kind="general", importance="supporting",
                            concepts=["memory-vs-compute", "residents"],
                            text="Loaded models hold GPU memory but use no compute until a request arrives."))
        rels.append(Relationship(id="r-res-gpu", level=2, source="residents", target="gpu", type="consumes",
                                 label="hold memory on"))
    hot = util is not None and util >= 70
    if not causes and not hot and known and (leases_p or leases_b):
        concepts.append(Concept(id="light-work", level=0, label="Leased work uses little compute", kind="state",
                                definition="GPU work holds leases but is waiting on memory, I/O or requests."))
        claims.append(Claim(id="c-cause-light", kind="inferred", importance="primary",
                            concepts=["light-work", "leases", "gpu-idle"],
                            evidence=["e-leases"] + (["e-util"] if util is not None else []),
                            text="GPU work holds leases but uses little compute while sampled, for example while "
                                 "it waits on memory, I/O or incoming requests."))
        chains.append(CausalChain(id="chain-light", label="Why the GPU is idle", claim="c-cause-light",
                                  steps=["light-work", "gpu-idle", "low-util"]))
        causes.append((0.6, "c-cause-light"))
    if not causes and not hot:
        concepts.append(Concept(id="no-demand", level=0, label="No GPU work queued", kind="state"))
        claims.append(Claim(id="c-cause-demand", kind="inferred", importance="primary",
                            concepts=["no-demand", "gpu-idle"], evidence=["e-leases"] if known else
                            [e.id for e in ev if e.id == "e-util"],
                            text="Nothing is asking for the GPU: no leases are active and no GPU pod is waiting."
                            if known else "Nothing appears to be asking for the GPU: no GPU pod is waiting."))
        chains.append(CausalChain(id="chain-demand", label="Why the GPU is idle", claim="c-cause-demand",
                                  steps=["no-demand", "gpu-idle", "low-util"]))
        causes.append((0.75 if not running else 0.6, "c-cause-demand"))
    if util is not None and util >= 70:
        claims.append(Claim(id="c-not-low", kind="observed", importance="primary", concepts=["gpu"],
                            evidence=["e-util"],
                            text=f"GPU utilization is {util:g}%, which is not low."))
        causes.insert(0, (0.95, "c-not-low"))
    causes.sort(key=lambda x: -x[0])
    head = causes[0][1]
    by = {c.id: c for c in claims}
    for strength, cid in causes:
        if by[cid].kind != "inferred":
            continue
        conf = round((strength if len(causes) == 1 else strength - 0.1) - (0 if known else 0.25), 2)
        by[cid].confidence = conf
        if cid != head:
            by[cid].importance = "supporting"
        reason = ("Inferred from one sample of state; no history was examined." if len(causes) == 1
                  else f"{len(causes)} possible causes are present at once.")
        if not known:
            reason += " gpusched could not be read, so leases and the primary-workload state are unknown."
        needed = ["utilization over the last hour", "gpusched lease history"]
        if not known and errors.get("gpusched"):
            needed.insert(0, "gpusched metrics (read failed)")
        uncs.append(Uncertainty(id=f"u-{cid[2:]}", about=cid, confidence=conf, reason=reason,
                                evidence_needed=needed))
    return KnowledgeModel(question=question, headline=head, concepts=concepts, claims=claims, evidence=ev,
                          relationships=rels, causal_chains=chains, uncertainties=uncs, source_refs=srcs,
                          builder="state:gpu", data_class="CONFIDENTIAL", source_snapshot=f"{ts:.0f}")


# ── llm builder ──────────────────────────────────────────────────────────────────────────────

Generate = Callable[[list[dict]], Awaitable[str]]


class Truncated(Exception):
    """The model stopped at its token limit, so its JSON is incomplete. The gateway caps completions while the
    primary workload is live (yield.max_tokens_busy), so this is expected, not a fault."""

LLM_INSTRUCTIONS = """You build a knowledge model for an explanation. Return ONLY one JSON object, no prose.
Schema (all ids lowercase-kebab, unique across the object):
{"question": str, "headline": <claim id that answers the question>,
 "concepts": [{"id","label","definition","kind": entity|state|event|quantity|process|idea}],
 "claims": [{"id","text","kind": definition|general|inferred|assumption,"concepts":[concept ids],
             "confidence": 0..1,"importance": primary|supporting|detail}],
 "relationships": [{"id","from": concept id,"to": concept id,"type": depends_on|causes|blocks|contains|routes_to|
                    precedes|consumes|produces|limits|uses|part_of|is_a,"label": short verb phrase}],
 "causal_chains": [{"id","label","steps":[concept ids, cause first],"claim": claim id}],
 "processes": [{"id","name","steps":[concept ids in order]}],
 "examples": [{"id","text","illustrates":[claim ids],"counter": bool}],
 "misconceptions": [{"id","text","correction": claim id}],
 "uncertainties": [{"id","about": claim id,"confidence": same as the claim,"reason": str}]}
Rules: short, plain sentences. Every claim names at least one concept. Any claim with confidence below 0.9 has an
uncertainty. Use only what you are confident is true; do not invent measurements.
Be compact: the whole object must stay under 350 tokens. 3-6 concepts, 2-5 claims, one short sentence each;
include only the lists that help this question (omit the rest). For a "how" or "teach me" question include one
process (its steps in order); for a "why" question include one causal_chain. Steps are concept ids."""
SHORTER = ("Your previous answer was cut off at the length limit. Return a smaller JSON object: at most 4 concepts "
           "and 3 claims, one short sentence each, and only the lists you need.")


_ELEMENT_TYPES = {"concepts": Concept, "claims": Claim, "relationships": Relationship, "causal_chains": CausalChain,
                  "processes": Process, "examples": Example, "misconceptions": Misconception,
                  "uncertainties": Uncertainty}
# None = drop the element (an edge of unknown type means nothing); otherwise the neutral value.
_ENUM_DEFAULTS = {"concepts": {"kind": "idea"}, "claims": {"kind": "general", "importance": "supporting"},
                  "relationships": {"type": None}}


def normalize(raw: dict) -> dict:
    """Deterministic fixes for the shape mistakes small models make. It never adds a fact: it re-points the
    headline at an existing claim, drops references to elements that do not exist, and records that an
    unexplained low confidence is unexplained."""
    raw = {k: v for k, v in raw.items() if k in KnowledgeModel.model_fields}
    for key, cls in _ELEMENT_TYPES.items():          # unknown keys out, unknown enum values to safe defaults
        items = []
        for it in raw.get(key) or []:
            if not isinstance(it, dict):
                continue
            it = {k: v for k, v in it.items() if k in cls.model_fields or k in ("from", "to")}
            for fld, default in _ENUM_DEFAULTS.get(key, {}).items():
                allowed = cls.model_fields[fld].annotation.__args__
                if fld in it and it[fld] not in allowed:
                    if default is None:
                        it = None
                        break
                    it[fld] = default
            if it is not None:
                items.append(it)
        raw[key] = items
    concepts = [c for c in raw.get("concepts") or [] if isinstance(c, dict) and c.get("id")]
    cids = {c["id"] for c in concepts}
    claims = []
    for c in raw.get("claims") or []:
        if not isinstance(c, dict) or not c.get("id") or not c.get("text"):
            continue
        c = {**c, "concepts": [x for x in c.get("concepts") or [] if x in cids]}
        if not c["concepts"] and concepts:
            continue                                   # an orphan claim: nothing to attach it to
        claims.append(c)
    ids = {c["id"] for c in claims}
    head = raw.get("headline")
    if head not in ids:
        by_text = next((c["id"] for c in claims if c["text"].strip().lower() == str(head or "").strip().lower()), None)
        primary = next((c["id"] for c in claims if c.get("importance") == "primary"), None)
        head = by_text or primary or (claims[0]["id"] if claims else head)
    raw.update(concepts=concepts, claims=claims, headline=head)
    raw["relationships"] = [r for r in raw.get("relationships") or []
                            if isinstance(r, dict) and r.get("from") in cids and r.get("to") in cids]
    chains = []
    for ch in raw.get("causal_chains") or []:
        steps = [s for s in (ch.get("steps") or []) if s in cids] if isinstance(ch, dict) else []
        if len(steps) >= 2:
            chains.append({**ch, "steps": steps, "claim": ch.get("claim") if ch.get("claim") in ids else ""})
    raw["causal_chains"] = chains
    raw["processes"] = [{**p, "steps": [s for s in p.get("steps") or [] if s in cids]}
                        for p in raw.get("processes") or [] if isinstance(p, dict)
                        and any(s in cids for s in p.get("steps") or [])]
    raw["examples"] = [{**e, "illustrates": [x for x in e.get("illustrates") or [] if x in ids | cids]}
                       for e in raw.get("examples") or [] if isinstance(e, dict) and e.get("id") and e.get("text")]
    raw["misconceptions"] = [m for m in raw.get("misconceptions") or [] if isinstance(m, dict)
                             and m.get("correction") in ids]
    uncs = [u for u in raw.get("uncertainties") or [] if isinstance(u, dict) and u.get("about") in ids]
    conf = {c["id"]: float(c.get("confidence", 1.0)) for c in claims}
    for u in uncs:
        u["confidence"] = conf[u["about"]]             # the claim's number is the one shown everywhere
    have = {u["about"] for u in uncs}
    for c in claims:
        if conf[c["id"]] < 0.9 and c["id"] not in have:
            uncs.append({"id": f"u-{c['id']}", "about": c["id"], "confidence": conf[c["id"]],
                         "reason": "The local model gave this lower confidence without saying why."})
    raw["uncertainties"] = uncs
    return raw


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in the model output")
    return json.loads(m.group(0))


async def from_llm(question: str, generate: Generate, context: str = "", model: str = "") -> KnowledgeModel:
    msgs = [{"role": "system", "content": LLM_INSTRUCTIONS},
            {"role": "user", "content": (f"Context:\n{context}\n\n" if context else "") + f"Question: {question}"}]
    last = ""
    repairs, cuts = 0, 0
    while repairs < 2 and cuts < 2:
        try:
            text = await generate(msgs)
        except Truncated:
            cuts += 1                                  # its own budget: a cut-off is not a schema mistake
            last = "the output was cut off at the model's token limit"
            msgs = [*msgs[:2], {"role": "user", "content": msgs[1]["content"] + "\n\n" + SHORTER}]
            continue
        repairs += 1
        try:
            raw = normalize(_extract_json(text))
            raw["question"] = question
            raw.setdefault("builder", f"llm:{model or 'local'}")
            raw["model"] = model
            km = KnowledgeModel.model_validate(raw)
            res = validate(plan(km))
            if res.ok:
                return km
            last = "; ".join(i.message for i in res.errors[:8])
        except (ValueError, ValidationError, KeyError) as e:
            last = str(e)[:800]
        msgs += [{"role": "assistant", "content": text},
                 {"role": "user", "content": f"That JSON is invalid: {last}. Return the corrected JSON object only."}]
    if "token limit" in last:
        raise ValueError("the local model could not fit an explanation in its token limit (completions are capped "
                         "while the primary workload is busy); try again later or use a shorter question")
    raise ValueError(f"model output did not validate after repair: {last}")
