"""Check for Better Models — Hugging Face discovery → screening → shortlist.

    HF search (per category)                               ~hundreds
      → deterministic metadata / license / format filters
      → hardware-fit (measured budgets on this DGX)
      → Jev screening DAG, all candidates in parallel        (suitability + risk = 1 Jev call,
                                                              improvement vs current = 1 call)
      → policy recommendation + category-weighted priority
      → shortlist per category → registry CANDIDATE          ~5 per category

Nothing is downloaded here. Downloads happen only for shortlisted CANDIDATEs, and only
when an operator (or models.automatic_download) asks for them.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from typing import Any

from lif.common import config, log
from lif.decision.dag import DagRuntime
from lif.decision.rules import model_suitability as _rule_suitability
from lif.decision.types import DecisionResult
from lif.models import hardware_fit, hf
from lif.models.registry import Registry
from lif.policy import engine as policy

LOG = log.get("lif.discovery")

# Search specs per portfolio category. Each category is optimized independently.
CATEGORIES: dict[str, dict[str, Any]] = {
    "fast":      {"queries": [{"pipeline_tag": "text-generation", "library": "gguf"}], "max_params_b": 4.5,
                  "min_params_b": 0.5, "tasks": {"text-generation"}},
    "general":   {"queries": [{"pipeline_tag": "text-generation", "library": "gguf"}], "max_params_b": 15,
                  "min_params_b": 3, "tasks": {"text-generation"}},
    "coding":    {"queries": [{"search": "coder", "library": "gguf"}], "max_params_b": 15, "min_params_b": 1,
                  "tasks": {"text-generation"}, "name_re": r"(?i)cod"},
    "reasoning": {"queries": [{"pipeline_tag": "text-generation", "library": "gguf", "search": "thinking"},
                              {"pipeline_tag": "text-generation", "library": "gguf", "search": "reason"}],
                  "max_params_b": 35, "min_params_b": 3, "tasks": {"text-generation"}},
    "embedding": {"queries": [{"pipeline_tag": "feature-extraction", "library": "gguf"},
                              {"search": "embedding", "library": "gguf"}],
                  "max_params_b": 8, "min_params_b": 0.05, "tasks": {"feature-extraction", "sentence-similarity",
                                                                     "text-generation", None}, "name_re": r"(?i)embed"},
    "reranking": {"queries": [{"search": "reranker", "library": "gguf"}], "max_params_b": 8, "min_params_b": 0.05,
                  "tasks": {"text-ranking", "text-classification", "text-generation", None}, "name_re": r"(?i)rerank"},
    # Vision GGUFs live in conversion repos (Qwen/*-GGUF, unsloth/*-GGUF tag image-text-to-text;
    # ggml-org/* often carry no pipeline_tag). The reliable gate is a sha-pinned `*mmproj*.gguf`
    # sibling (see requires_mmproj), not the tag. 1–9B keeps CPU decode useful on the A725 cores.
    "vision":    {"queries": [{"pipeline_tag": "image-text-to-text", "search": "GGUF"},
                              {"search": "VL", "library": "gguf"},
                              {"search": "vision", "library": "gguf"}],
                  "max_params_b": 9, "min_params_b": 1, "tasks": {"image-text-to-text", "text-generation", None},
                  "name_re": r"(?i)(?:\bvl|vl\b|vlm|vision|omni|llava|pixtral|minicpm-v|internvl|gemma-3)",
                  "name_re_bypass_tags": {"image-text-to-text"}, "requires_mmproj": True},
    # Grounded answers (local/web). Operator-nominated only (`local-ai models nominate`): which models read
    # sources well is measured by the web suite, not guessed from metadata, and MoE models (sized by their
    # ACTIVE parameters, hardware_fit) are in range up to 36B total.
    "web":       {"queries": [], "max_params_b": 36, "min_params_b": 3, "nominate_only": True,
                  "tasks": {"text-generation", "image-text-to-text", None}},
}

# Category-specific priority weights (spec §21). Inputs are normalized to 0..1.
WEIGHTS = {
    "fast":      {"suitability": 0.30, "improvement": 0.20, "speed": 0.25, "memory": 0.15, "reliability": 0.10},
    "general":   {"suitability": 0.35, "improvement": 0.25, "speed": 0.10, "memory": 0.10, "reliability": 0.20},
    "coding":    {"suitability": 0.35, "improvement": 0.25, "speed": 0.10, "memory": 0.10, "reliability": 0.20},
    "reasoning": {"suitability": 0.35, "improvement": 0.30, "speed": 0.05, "memory": 0.10, "reliability": 0.20},
    "embedding": {"suitability": 0.30, "improvement": 0.20, "speed": 0.20, "memory": 0.15, "reliability": 0.15},
    "reranking": {"suitability": 0.30, "improvement": 0.20, "speed": 0.20, "memory": 0.15, "reliability": 0.15},
    "vision":    {"suitability": 0.35, "improvement": 0.25, "speed": 0.10, "memory": 0.15, "reliability": 0.15},
    "web":       {"suitability": 0.40, "improvement": 0.30, "speed": 0.10, "memory": 0.05, "reliability": 0.15},
}

ADVANCE_SHORTLIST = 0.70      # P(benchmark)+P(candidate) that shortlists on Jev alone
ADVANCE_WITH_RULES = 0.50     # …or this much, when the deterministic rule independently agrees

class HFUnavailable(Exception):
    pass


_TOY = re.compile(r"(?i)(tiny-random|\btest\b|dummy|debug|-merge|frankenmerge|abliterat|uncensor|lora\b|adapter)")


def profile_id(meta: dict, device: str) -> str:
    base = meta["model_id"].split("/")[-1].lower().replace("_", "-").replace(".", "-")
    base = re.sub(r"-gguf$", "", base)
    q = ((meta.get("gguf_pick") or {}).get("quant") or "bf16").lower().replace("_", "")
    return f"{base}-{q}-{device}"[:80]


# ── deterministic filter ─────────────────────────────────────────────────────

def metadata_filter(meta: dict, category: str, blocked: set[str], stage: str = "full") -> str | None:
    """Returns a rejection reason, or None if the model passes.

    stage="listing" runs on search-listing fields only (no sizes/hashes/params yet) and
    lets unknown values through; stage="full" runs after the detail fetch."""
    spec = CATEGORIES[category]
    allowed = set(config.get("models.discovery.allowed_licenses") or [])
    mid = meta["model_id"]
    if not meta.get("revision") or not re.match(r"^[0-9a-f]{40}$", meta["revision"]):
        return "no pinned revision"
    if mid in blocked or mid.split("/")[0] in set(config.get("models.discovery.blocked_orgs") or []):
        return "blocked by operator"
    if _TOY.search(mid):
        return "toy/test/merge/adapter"
    if spec.get("name_re") and not re.search(spec["name_re"], mid) and \
            meta.get("pipeline_tag") not in spec.get("name_re_bypass_tags", ()):
        return "name does not match category"
    if meta.get("pipeline_tag") not in spec["tasks"]:
        return f"pipeline_tag {meta.get('pipeline_tag')} not in category"
    if meta["downloads"] < int(config.get("models.discovery.min_downloads", 1000)):
        return "too few downloads"
    lic = (meta.get("license") or "").lower()
    if not lic:
        return "license unknown"
    if lic not in allowed and not any(lic.startswith(a) for a in allowed):
        return f"license {lic} not allowed"
    if meta.get("gated"):
        return "gated (manual review required)"
    if meta.get("trust_remote_code"):
        return "requires trust_remote_code"
    pb = meta.get("params_b")
    if pb is None:
        return None if stage == "listing" else "parameter count unknown"
    if not (spec["min_params_b"] <= pb <= spec["max_params_b"]):
        return f"{pb}B outside category range {spec['min_params_b']}–{spec['max_params_b']}B"
    if stage == "listing":
        if spec.get("requires_mmproj") and meta.get("mmproj_listed") is False:
            return NO_PROJECTOR
        return None
    if not meta.get("gguf_pick"):
        return "no usable GGUF quant (Q4_K_M/Q5_K_M/Q8_0) with checksum"
    if spec.get("requires_mmproj") and not _mmproj_ok(meta["gguf_pick"].get("mmproj")):
        return NO_PROJECTOR
    return None


NO_PROJECTOR = "no image projector"      # no Q8_0/F16/BF16 *mmproj*.gguf with size + sha256


def _mmproj_ok(mm: dict | None) -> bool:
    return bool(mm and mm.get("file") and mm.get("size") and
                re.match(r"^[0-9a-f]{64}$", str(mm.get("sha256") or "")))


# ── DAG node functions ───────────────────────────────────────────────────────

def build_dag(runtime: DagRuntime, current_by_category: dict[str, dict]) -> None:
    @runtime.register("license_ok")
    def license_ok(ctx):
        lic = (ctx["input"].get("license") or "").lower()
        return {"ok": bool(lic), "license": lic}

    @runtime.register("architecture_fit")
    def architecture_fit(ctx):
        f = ctx["input"].get("hardware_fit")
        return {"ok": f in ("fits_cpu", "fits_gpu_now", "fits_when_gramz_unloaded"), "verdict": f}

    @runtime.register("build_comparison")
    def build_comparison(ctx):
        cur = current_by_category.get(ctx["input"]["category"]) or {}
        keep = ("model_id", "params_b", "license", "last_modified", "downloads", "family", "context_length")
        return {"category": ctx["input"]["category"],
                "candidate": {k: ctx["input"].get(k) for k in keep},
                "current": {k: cur.get(k) for k in keep} if cur else None}

    @runtime.register("candidate_recommendation")
    def candidate_recommendation(ctx):
        suit: DecisionResult = ctx["suitability"]
        risk: DecisionResult = ctx["operational_risk"]
        imp: DecisionResult = ctx["improvement"]
        if not ctx["license"]["ok"]:
            return {"action": "reject", "why": "license"}
        if not ctx["architecture_fit"]["ok"]:
            return {"action": "reject", "why": "hardware fit"}
        if risk.decision == "high" and risk.action in (policy.Gate.AUTO, policy.Gate.VALIDATE):
            return {"action": "reject", "why": "high operational risk"}
        if suit.decision == "reject" and suit.action in (policy.Gate.AUTO, policy.Gate.VALIDATE):
            return {"action": "reject", "why": "unsuitable"}
        if imp.decision in ("very_unlikely", "unlikely") and imp.action in (policy.Gate.AUTO, policy.Gate.VALIDATE):
            return {"action": "hold", "why": "unlikely to improve on current"}
        # Use the probability MASS on advancing, not the argmax: Jev's 4-way distributions
        # are often flat, and a 0.44 "benchmark" argmax is not a decision.
        p = suit.probabilities or {suit.decision: suit.confidence}
        advance = p.get("benchmark", 0.0) + p.get("candidate", 0.0)
        rule = _rule_suitability(ctx["input"])
        rule_agrees = rule is not None and rule[0] in ("benchmark", "candidate")
        if suit.provider == "rules":
            advance = 1.0 if rule_agrees else 0.0
        if advance >= ADVANCE_SHORTLIST:
            return {"action": "shortlist", "why": f"P(advance)={advance:.2f} ({suit.provider})", "p_advance": advance}
        if advance >= ADVANCE_WITH_RULES and rule_agrees:
            return {"action": "shortlist", "why": f"P(advance)={advance:.2f} + deterministic validation agrees",
                    "p_advance": advance}
        return {"action": "review", "why": f"P(advance)={advance:.2f}; no independent agreement", "p_advance": advance}


def priority_score(meta: dict, fit: dict, run: dict, category: str) -> float:
    w = WEIGHTS.get(category, WEIGHTS["general"])
    suit: DecisionResult = run["suitability"]
    imp: DecisionResult = run["improvement"]
    risk: DecisionResult = run["operational_risk"]
    p = suit.probabilities or {suit.decision: suit.confidence}
    suitability = p.get("candidate", 0) * 1.0 + p.get("benchmark", 0) * 0.75 + p.get("review", 0) * 0.3
    improvement = (imp.score / 4.0) if imp.score is not None else 0.5
    reliability = {"low": 1.0, "moderate": 0.5, "high": 0.0}.get(risk.decision, 0.3)
    tps = fit.get("est_cpu_decode_tps") or 5
    speed = min(1.0, tps / 40.0)
    memory = max(0.0, 1.0 - (fit.get("total_mib") or 0) / 40000)
    return round(w["suitability"] * suitability + w["improvement"] * improvement + w["speed"] * speed
                 + w["memory"] * memory + w["reliability"] * reliability, 4)


# ── the pipeline ─────────────────────────────────────────────────────────────

class Discovery:
    def __init__(self, registry: Registry, dag: DagRuntime, hfc: hf.HFClient | None = None,
                 admissible_mib=lambda: 0.0, observer=None):
        self.reg, self.dag, self.hf = registry, dag, hfc or hf.HFClient()
        self.admissible = admissible_mib
        self.running: int | None = None
        # Decision Engineering: async fn(payload) that reports each advance decision to
        # POST /de/observe (shadow + human review). Never awaited on the critical path's result.
        self.observer = observer

    def current_models(self) -> dict[str, dict]:
        out = {}
        for m in self.reg.list(["PRODUCTION"]):
            out.setdefault(m["category"], m["meta"] or {})
        return out

    async def run(self, categories: list[str] | None = None, actor: str = "operator") -> dict:
        if self.running:
            return {"status": "already_running", "run": self.running}
        if self.reg.setting("discovery_disabled", False):
            return {"status": "disabled", "reason": "discovery disabled by operator"}
        if actor != "operator" and self.reg.setting("maintenance", False):
            return {"status": "disabled", "reason": "maintenance mode: autonomous discovery paused"}
        cats = categories or [c for c, v in CATEGORIES.items() if not v.get("nominate_only")]
        run_id = self.reg.start_discovery()
        self.running = run_id
        t0 = time.perf_counter()
        funnel: dict[str, Any] = {"categories": {}}
        self.reg.event("discovery_started", f"run {run_id}", actor, categories=cats)
        try:
            blocked = {m["model_id"] for m in self.reg.list() if m["blocked"]}
            known = {(m["model_id"], m["revision"]) for m in self.reg.list()}
            current = self.current_models()
            build_dag(self.dag, current)
            per_list = int(config.get("models.discovery.max_listed_per_category", 200))
            shortlist_n = int(config.get("models.discovery.shortlist_per_category", 5))

            # 1. list all categories concurrently
            listings = await asyncio.gather(*(self._list(c, per_list) for c in cats), return_exceptions=True)
            unreachable = [str(x) for x in listings if isinstance(x, HFUnavailable)]
            if len(unreachable) == len(cats):
                raise HFUnavailable(unreachable[0])
            for cat, infos in zip(cats, listings):
                if isinstance(infos, Exception):
                    funnel["categories"][cat] = {"error": str(infos)[:200]}
                    continue
                f = funnel["categories"][cat] = {"listed": len(infos)}
                rejects: Counter = Counter()
                survivors: list[tuple[dict, dict]] = []
                # 1a. cheap filter on listing fields, then fetch details (sizes, hashes,
                #     GGUF header) only for the survivors — bounded, most-downloaded first
                cheap = []
                for info in infos:
                    why = metadata_filter(hf.normalize(info, cat), cat, blocked, stage="listing")
                    if why:
                        rejects[_bucket(why)] += 1
                    else:
                        cheap.append(info)
                cheap.sort(key=lambda i: -(i.get("downloads") or 0))
                detail_cap = int(config.get("models.discovery.detail_fetch_per_category", 40))
                f["after_listing_filter"] = len(cheap)
                details = await asyncio.gather(*(self.hf.model_info(i["id"]) for i in cheap[:detail_cap]),
                                               return_exceptions=True)
                f["detail_fetched"] = sum(1 for d in details if not isinstance(d, Exception))
                for info in details:
                    if isinstance(info, Exception):
                        rejects["metadata fetch failed"] += 1
                        continue
                    meta = hf.normalize(info, cat)
                    why = metadata_filter(meta, cat, blocked)
                    if why:
                        rejects[_bucket(why)] += 1
                        continue
                    fit = hardware_fit.estimate(meta, context=8192, concurrency=4,
                                                admissible_mib=self.admissible()).to_dict()
                    if fit["verdict"] == "no_fit":
                        rejects["hardware fit"] += 1
                        continue
                    meta["hardware_fit"] = fit["verdict"]
                    meta["runtime_compatibility"] = "llama.cpp (cpu)" if fit["verdict"] == "fits_cpu" else \
                        "llama.cpp-cuda (gpusched window)"
                    survivors.append((meta, fit))
                f["after_deterministic"] = len(survivors) + 0
                f["rejected"] = dict(rejects.most_common(8))

                # 2. Jev screening DAG for every survivor, concurrently
                t_screen = time.perf_counter()
                runs = await asyncio.gather(*(self.dag.run("candidate-model-analysis", _screen_state(m))
                                              for m, _ in survivors), return_exceptions=True)
                f["screen_ms"] = round((time.perf_counter() - t_screen) * 1000)
                f["jev_calls"] = sum(r.jev_calls for r in runs if not isinstance(r, Exception))
                f["screen_errors"] = sum(1 for r in runs if isinstance(r, Exception))
                scored = []
                for (meta, fit), r in zip(survivors, runs):
                    if isinstance(r, Exception):
                        continue
                    rec = r.results["recommendation"]
                    screening = {"recommendation": rec,
                                 "suitability": r.results["suitability"].to_dict(),
                                 "operational_risk": r.results["operational_risk"].to_dict(),
                                 "improvement": r.results["improvement"].to_dict(),
                                 "dag_ms": round(r.total_ms), "jev_calls": r.jev_calls}
                    if rec["action"] in ("shortlist", "review"):
                        scored.append((priority_score(meta, fit, r.results, cat), meta, fit, screening))
                f["after_screening"] = len(scored)
                if self.observer is not None:
                    f["observed"] = await self._observe(cat, current.get(cat), survivors, runs)
                providers = Counter(r.results["suitability"].provider for r in runs if not isinstance(r, Exception))
                f["providers"] = dict(providers)

                # 3. prioritize → shortlist
                # one entry per base model: repacks (bartowski/lmstudio/…) of the same weights
                # compete for the same slot; the first-party repo wins ties
                scored.sort(key=lambda x: (-x[0], not _first_party(x[1])))
                seen = {_base_key(m["model_id"]) for m in self.reg.list() if m["category"] == cat}
                short = []
                for item in scored:
                    if item[3]["recommendation"]["action"] != "shortlist":
                        continue
                    k = _base_key(item[1]["model_id"])
                    if k in seen:
                        continue
                    seen.add(k)
                    short.append(item)
                    if len(short) >= shortlist_n:
                        break
                f["shortlisted"] = [s[1]["model_id"] for s in short]
                for score, meta, fit, screening in short:
                    if (meta["model_id"], meta["revision"]) in known:
                        continue
                    screening["priority"] = score
                    self._register(meta, fit, screening, actor)
            funnel["total_ms"] = round((time.perf_counter() - t0) * 1000)
            self.reg.finish_discovery(run_id, "succeeded", funnel)
            self.reg.event("discovery_finished", f"run {run_id}", actor,
                           shortlisted=sum(len(c.get("shortlisted", [])) for c in funnel["categories"].values()),
                           total_ms=funnel["total_ms"])
            return {"status": "succeeded", "run": run_id, "funnel": funnel}
        except Exception as exc:
            LOG.exception("discovery failed")
            self.reg.finish_discovery(run_id, "failed", funnel, str(exc))
            self.reg.event("discovery_failed", f"run {run_id}", actor, error=str(exc)[:300])
            return {"status": "failed", "run": run_id, "error": str(exc), "funnel": funnel}
        finally:
            self.running = None

    async def _observe(self, cat: str, cur: dict | None, survivors, runs) -> int:
        """Report the agent's own advance decision for every screened model to the shadow API.
        Bounded, concurrent, and failure-proof: discovery never depends on it."""
        payloads = []
        for (meta, _), r in zip(survivors, runs):
            if isinstance(r, Exception):
                continue
            rec, suit = r.results["recommendation"], r.results["suitability"]
            payloads.append({"decision": "model-advance", "agent": "model-discovery", "workflow": "discovery",
                             "data_class": "PUBLIC", "state": advance_state(meta, cat, cur),
                             "baseline": {"answer": "yes" if rec["action"] == "shortlist" else "no",
                                          "executor": f"discovery-policy:{suit.provider}",
                                          "confidence": float(rec.get("p_advance") or 0.0)}})

        async def one(p):
            try:
                await asyncio.wait_for(self.observer(p), timeout=20)
                return 1
            except Exception:
                return 0
        return sum(await asyncio.gather(*(one(p) for p in payloads)))

    async def _list(self, cat: str, limit: int) -> list[dict]:
        """Raises HFUnavailable if EVERY query for the category failed — an outage must
        never look like 'no models found'."""
        seen: dict[str, dict] = {}
        errors = []
        for q in CATEGORIES[cat]["queries"]:
            try:
                for info in await self.hf.search(limit=limit, **q):
                    seen.setdefault(info["id"], info)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {str(exc)[:120]}")
                LOG.warning("hf search failed", extra={"fields": {"category": cat, "err": str(exc)[:160]}})
        if errors and len(errors) == len(CATEGORIES[cat]["queries"]):
            raise HFUnavailable(f"Hugging Face unreachable for '{cat}': {errors[0]}")
        return list(seen.values())

    def _register(self, meta: dict, fit: dict, screening: dict, actor: str) -> None:
        device = "cpu" if fit["verdict"] == "fits_cpu" else "gpu"
        pick = meta["gguf_pick"]
        prof = {"category": meta["category"], "runtime": "llama.cpp", "device": device,
                "hf_repo": meta["model_id"], "revision": meta["revision"], "file": pick["file"],
                "sha256": pick["sha256"], "size": pick["size"], "precision": (pick.get("quant") or "").lower(),
                "params_b": meta["params_b"], "context": 8192, "concurrency": 4,
                "weights_mib": fit["weights_mib"], "anon_mib": fit["anon_mib"],
                "memory_budget_mb": hardware_fit.memory_limit_mib(fit["weights_mib"], fit["anon_mib"]),
                "license": meta["license"]}
        if pick.get("mmproj"):
            mm = pick["mmproj"]
            prof["mmproj"] = {"file": mm["file"], "sha256": mm["sha256"], "size": mm["size"]}
        mid = profile_id(meta, device)
        self.reg.upsert(mid, meta["model_id"], meta["revision"], meta["category"], "DISCOVERED",
                        meta=meta, profile=prof, fit=fit, screening=screening, actor=actor,
                        reason="discovered by Check for Better Models")
        self.reg.transition(mid, "CANDIDATE", f"shortlisted (priority {screening['priority']}): "
                                              f"{screening['recommendation']['why']}", actor)


def _base_key(model_id: str) -> str:
    """'bartowski/Qwen2.5-Coder-7B-Instruct-GGUF' → 'qwen2.5-coder-7b-instruct';
    'bartowski/Qwen_Qwen3.5-4B-GGUF' and 'unsloth/Qwen3.5-4B-GGUF' → 'qwen3.5-4b'."""
    name = model_id.split("/")[-1].lower()
    name = re.sub(r"^[^_/-]+_(?=.)", "", name)    # bartowski-style 'Qwen_Qwen3.5-4B' → 'qwen3.5-4b'
    return re.sub(r"(-gguf|-q\d.*|-i?q\d_.*|-qat.*|-gguf-.*)$", "", name)


def _first_party(meta: dict) -> bool:
    base = meta.get("base_model")
    base = base[0] if isinstance(base, list) and base else base
    return bool(base) and isinstance(base, str) and base.split("/")[0].lower() == meta["model_id"].split("/")[0].lower()


def _bucket(why: str) -> str:
    """Group rejection reasons for the funnel (drop per-model numbers)."""
    for prefix in ("pipeline_tag", "license", "parameter", "no usable GGUF"):
        if why.startswith(prefix):
            return prefix
    return re.sub(r"[\d.]+B outside.*", "size outside category range", why)


def advance_state(meta: dict, cat: str, cur: dict | None) -> dict:
    """State for the model-advance decision. Comparisons with production are computed here
    (rule 6: never ask the model to compare numbers or dates)."""
    cur = cur or {}
    cp, up = float(meta.get("params_b") or 0), float(cur.get("params_b") or 0)
    return {"model": {"id": meta.get("model_id", ""), "pipeline_tag": meta.get("pipeline_tag") or "",
                      "tags": [str(t) for t in (meta.get("tags") or [])][:20]},
            "category": {"name": cat},
            "current": {"model_id": cur.get("model_id") or "none"},
            "comparison": {"newer_than_current": str(meta.get("last_modified") or "") > str(cur.get("last_modified") or ""),
                           "larger_than_current": bool(cur) and cp > up * 1.2}}


def _screen_state(meta: dict) -> dict:
    """What Jev sees: public HF metadata only (no local paths, no tokens)."""
    keys = ("model_id", "category", "architecture", "family", "params_b", "license", "pipeline_tag", "downloads",
            "likes", "last_modified", "created_at", "context_length", "safetensors", "gguf", "trust_remote_code",
            "tags", "base_model", "hardware_fit", "runtime_compatibility")
    return {k: meta.get(k) for k in keys}
