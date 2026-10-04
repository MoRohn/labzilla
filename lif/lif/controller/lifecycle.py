"""Model lifecycle operations (controller-side).

    CANDIDATE → download (sha256-pinned Job) → STAGED → benchmark (temporary CPU server,
    real local evidence) → JEV_COMPARE (advisory) + policy compare → APPROVED | REJECTED
    → canary (N % of an alias) → PRODUCTION (promote) → rollback any time

Gates that are code, not AI:
  * nothing downloads without a pinned commit sha + file sha256
  * candidate benchmarks start only when the primary workload is LOW/MODERATE AND the host keeps
    gpusched's 8 GiB headroom after the candidate's anonymous memory; they abort the
    moment the primary workload becomes IMMINENT
  * promotion needs a benchmark that passed every threshold in models.promotion
  * production / rollback-critical / pinned artifacts are never deleted
"""
from __future__ import annotations

import asyncio
import json
import hashlib
import time
from typing import Any

from lif.common import config, log
from lif.controller import templates
from lif.controller.k8s import K8s
from lif.decision.fabric import DecisionFabric
from lif.gpu.state import BlerbzState, GpuStateWatcher
from lif.models import evaluator
from lif.models.registry import Registry

LOG = log.get("lif.lifecycle")

CATEGORY_ALIAS = {"fast": "local/fast", "general": "local/default", "coding": "local/code",
                  "reasoning": "local/reasoning", "embedding": "local/embedding", "reranking": "local/rerank",
                  "vision": "local/vision", "web": "local/web"}
HEADROOM_MIB = 8192 + 1024       # gpusched safety headroom + margin


def suite_for(category: str | None) -> str:
    """Benchmark suite per category: vision models are graded on images, embedders on sanity."""
    if category in ("embedding", "reranking"):
        return "embedding"
    return {"vision": "vision", "web": "web"}.get(category or "", "core")


class OpError(Exception):
    pass


def _resident_mib(m: dict) -> int:
    """Memory a server will hold: its anonymous memory plus the weights it keeps hot. The weights are
    page cache that MemAvailable counts as free, but a serving model touches them every token, so
    reclaiming them thrashes (tier0, 2026-10-01). Small models were gated on anon alone; a 12 GiB
    MoE model can't be."""
    fit, prof = m.get("fit") or {}, m.get("profile") or {}
    anon = int(fit.get("anon_mib") or prof.get("anon_mib") or 1024)
    weights = int(fit.get("weights_mib") or prof.get("weights_mib") or 0)
    return anon + (weights if weights >= LARGE_WEIGHTS_MIB else 0)


LARGE_WEIGHTS_MIB = 6144         # at or above this, weights count toward the start-up headroom gate


def deployment_name(mid: str, profile: dict) -> str:
    """Seeded profiles keep their hand-written Deployment (endpoint host); controller-made
    servers are named lif-<hash>."""
    ep = profile.get("endpoint") or ""
    if ep.startswith("http://") and ".ai-serving.svc" in ep:
        return ep.split("//", 1)[1].split(".", 1)[0]
    return "lif-" + hashlib.sha1(mid.encode()).hexdigest()[:10]


class Lifecycle:
    def __init__(self, reg: Registry, k8s: K8s, gpu: GpuStateWatcher, fabric: DecisionFabric):
        self.reg, self.k8s, self.gpu, self.fabric = reg, k8s, gpu, fabric
        self.tasks: dict[str, asyncio.Task] = {}
        self.last_blerbz: BlerbzState | None = None
        # deployment → shed by the memory guard (persisted: a restart must still restore it)
        self.guard_shed: dict[str, bool] = dict(reg.setting("memory_guard_shed", {}) or {})
        self.guard_since: dict[str, float] = {}         # condition → first time it held

    # ── seed ─────────────────────────────────────────────────────────────────

    def seed(self) -> None:
        if self.reg.list():
            return
        cat = config.catalogue()
        for mid, p in (cat.get("profiles") or {}).items():
            self.reg.upsert(mid, p["hf_repo"], p["revision"], p["category"], "PRODUCTION",
                            meta={"model_id": p["hf_repo"], "params_b": p.get("params_b"), "license": p.get("license")},
                            profile={**p, "sha256": p.get("sha256")}, actor="bootstrap",
                            reason="seeded from config/models.yaml (deployed and benchmarked 2026-10-01)")
        for alias, chain in (cat.get("aliases") or {}).items():
            if chain:
                self.reg.set_alias(alias, chain, "bootstrap", note="seed")

    def routing_table(self) -> dict:
        serving = {m["id"]: m for m in self.reg.list(["PRODUCTION", "CANARY", "STANDBY", "APPROVED"])}
        profiles = {mid: m["profile"] for mid, m in serving.items() if m["profile"].get("endpoint")}
        aliases, canaries = {}, {}
        for a, v in self.reg.aliases().items():
            aliases[a] = [p for p in v["chain"] if p in profiles]
            if v.get("canary") and v["canary"].get("profile") in profiles:
                canaries[a] = v["canary"]
        for a in (config.catalogue().get("aliases") or {}):
            aliases.setdefault(a, [])
        return {"profiles": profiles, "aliases": aliases, "canaries": canaries,
                "jev_enabled": not self.reg.setting("jev_disabled", False),
                "alias_min_params_b": config.catalogue().get("alias_min_params_b") or {},
                "generated": time.time()}

    # ── background task helper ───────────────────────────────────────────────

    def _spawn(self, key: str, coro) -> dict:
        if key in self.tasks and not self.tasks[key].done():
            coro.close()
            return {"status": "already_running", "task": key}
        t = asyncio.create_task(coro)
        self.tasks[key] = t
        return {"status": "started", "task": key}

    def task_status(self) -> dict:
        return {k: ("running" if not t.done() else ("failed: " + str(t.exception())[:200] if t.exception()
                                                     else "done")) for k, t in self.tasks.items()}

    # ── download ─────────────────────────────────────────────────────────────

    def download(self, mid: str, actor: str) -> dict:
        m = self._get(mid)
        if m["state"] != "CANDIDATE":
            raise OpError(f"{mid} is {m['state']}; only CANDIDATE models are downloaded")
        if actor != "operator" and not self.reg.setting("automatic_download", config.get("models.automatic_download")):
            raise OpError("automatic downloads are disabled")
        if actor != "operator" and self.reg.setting("maintenance", False):
            raise OpError("maintenance mode")
        templates.validate_profile(m["profile"])
        if not m["profile"].get("sha256"):
            raise OpError("no sha256 for the artifact; refusing to download unverifiable weights")
        if m["profile"].get("mmproj") and not m["profile"]["mmproj"].get("sha256"):
            raise OpError("no sha256 for the image projector; refusing to download unverifiable files")
        return self._spawn(f"download:{mid}", self._download(mid, actor))

    async def _download(self, mid: str, actor: str) -> None:
        m = self._get(mid)
        name = "dl-" + hashlib.sha1(mid.encode()).hexdigest()[:10]
        self.reg.transition(mid, "DOWNLOADING", f"job {name}", actor)
        try:
            if not await self.k8s.job("ai-serving", name):
                await self.k8s.create_job("ai-serving", templates.download_job(name, m["profile"]))
            self.reg.event("download_started", mid, actor, job=name,
                           size=int(m["profile"].get("size") or 0) + int((m["profile"].get("mmproj") or {}).get("size") or 0),
                           files=[f for f, _ in templates.artifacts(m["profile"])])
            while True:
                await asyncio.sleep(10)
                j = await self.k8s.job("ai-serving", name) or {}
                st = j.get("status") or {}
                if st.get("succeeded"):
                    self.reg.transition(mid, "STAGED", f"downloaded + sha256 verified (revision "
                                                       f"{m['revision'][:12]} pinned)", actor)
                    self.reg.event("download_complete", mid, actor, job=name)
                    return
                if any(c.get("type") == "Failed" and c.get("status") == "True" for c in st.get("conditions") or []):
                    logs = await self.k8s.pod_logs("ai-serving", f"job-name={name}")
                    bad = "MISMATCH" in logs
                    self.reg.transition(mid, "QUARANTINED" if bad else "FAILED",
                                        "sha256 mismatch — artifact quarantined" if bad else
                                        f"download failed: {logs[-200:]}", actor)
                    return
        except Exception as exc:
            self.reg.transition(mid, "FAILED", f"download error: {exc}", actor, force=True)
            raise

    # ── benchmark ────────────────────────────────────────────────────────────

    def benchmark(self, mid: str, actor: str, suite: str | None = None) -> dict:
        """`suite`: run another suite on a live model, e.g. the web suite on local/default's model so a
        local/web candidate has a baseline to beat (see compare)."""
        m = self._get(mid)
        if suite is not None and suite not in ("core", "vision", "web", "embedding"):
            raise OpError(f"unknown suite {suite!r}")
        if m["state"] not in ("STAGED", "APPROVED", "STANDBY", "PRODUCTION", "CANARY"):
            raise OpError(f"{mid} is {m['state']}; benchmark needs a downloaded (STAGED) model")
        if m["profile"].get("device") != "cpu":
            raise OpError("GPU-tier benchmarks need a gpusched command-job window (see GPU_SCHEDULING.md)")
        snap = self.gpu.current()
        live = m["state"] in ("PRODUCTION", "CANARY", "STANDBY", "APPROVED") and m["profile"].get("endpoint")
        if snap.state >= BlerbzState.HIGH:      # evaluation is P6: never while the primary workload is HIGH/IMMINENT
            raise OpError(f"Primary workload {snap.state.name} ({snap.reason}); benchmarks deferred")
        if not live:
            if self.reg.setting("maintenance", False):
                raise OpError("maintenance mode")
            need = _resident_mib(m)
            if snap.mem_available_mib - need < HEADROOM_MIB:
                raise OpError(f"not enough host headroom: MemAvailable {snap.mem_available_mib:.0f} MiB − candidate "
                              f"~{need} MiB < {HEADROOM_MIB} MiB (gpusched safety headroom + margin)")
        if suite is not None and suite != suite_for(m["category"]) and not live:
            raise OpError("another suite runs only on a live model; a candidate is benchmarked on its own category")
        return self._spawn(f"benchmark:{mid}", self._benchmark(mid, actor, bool(live), suite))

    async def _benchmark(self, mid: str, actor: str, live: bool, suite_override: str | None = None) -> dict:
        m = self._get(mid)
        p = m["profile"]
        temp = None
        if not live:
            self.reg.transition(mid, "VALIDATING", "load test: starting temporary CPU server", actor)
            temp = "cand-" + hashlib.sha1(mid.encode()).hexdigest()[:10]
            dep, svc = templates.model_server(temp, mid, {**p, "concurrency": 2}, priority="ai-maintenance",
                                              role="candidate")
            await self.k8s.apply_deployment("ai-serving", dep)
            await self.k8s.apply_service("ai-serving", svc)
            url = f"http://{temp}.ai-serving.svc:8080"
        else:
            url = p["endpoint"]
        try:
            if temp:
                t0 = time.time()
                while time.time() - t0 < 600:
                    d = await self.k8s.deployment("ai-serving", temp) or {}
                    if (d.get("status") or {}).get("readyReplicas"):
                        break
                    if self.gpu.current().state == BlerbzState.IMMINENT:
                        raise OpError("Primary workload became IMMINENT during load; candidate stopped")
                    await asyncio.sleep(5)
                else:
                    raise OpError("load timeout (600 s)")
                self.reg.event("load_test_passed", mid, actor, load_sec=round(time.time() - t0, 1))
                self.reg.transition(mid, "BENCHMARKING", "functional + quality benchmark", actor)
            stop = lambda: self.gpu.current().state == BlerbzState.IMMINENT     # live or candidate
            extra = {"chat_template_kwargs": p["chat_template_kwargs"]} if p.get("chat_template_kwargs") else {}
            suite = suite_override or suite_for(p.get("category"))
            if suite == "embedding":
                results, summary = await embed_benchmark(url)
            else:
                results, summary = await evaluator.run_suite(url, model=mid, suite=suite, concurrency=2,
                                                             extra=extra, should_stop=stop)
            if summary.get("aborted"):
                self.reg.transition(mid, "STAGED", "benchmark aborted: the primary workload reclaimed capacity", actor)
                return summary
            self.reg.add_benchmark(mid, suite, results, summary)
            self.reg.event("benchmark_complete", mid, actor, suite=suite, quality=summary.get("quality"),
                           latency_ms_p50=summary.get("latency_ms_p50"))
            if live:
                return summary
            report = await self.compare(mid, summary)
            to = "APPROVED" if report["recommendation"] in ("CANARY", "HOLD") else "REJECTED"
            self.reg.transition(mid, to, f"benchmark → {report['recommendation']}"
                                         + (f" (failed: {', '.join(report.get('failed_checks') or [])})"
                                            if report.get("failed_checks") else ""), actor)
            self.reg.upsert(mid, m["model_id"], m["revision"], m["category"], to,
                            screening={**m["screening"], "comparison": report}, actor=actor)
            return summary
        except Exception as exc:
            cur = self._get(mid)["state"]
            if cur in ("VALIDATING", "BENCHMARKING"):
                self.reg.transition(mid, "FAILED" if cur == "VALIDATING" else "STAGED", f"benchmark error: {exc}",
                                    actor, force=True)
            raise
        finally:
            if temp:
                await self.k8s.delete_deployment("ai-serving", temp)
                await self.k8s.delete_service("ai-serving", temp)

    async def compare(self, mid: str, summary: dict) -> dict:
        m = self._get(mid)
        alias = CATEGORY_ALIAS.get(m["category"], "local/default")
        cur_alias = self.reg.alias(alias)
        incumbent = cur_alias["chain"][0] if cur_alias and cur_alias["chain"] else None
        if m["category"] == "web":
            return self._compare_web(mid, summary, incumbent)
        inc_b = self.reg.latest_benchmark(incumbent, suite_for(m["category"])) if incumbent else None
        inc_m = self.reg.get(incumbent) if incumbent else None
        if "quality" not in summary:      # embedding: latency/sanity only
            ok = summary.get("sanity_pass") and not summary.get("errors")
            inc_dim = (inc_m["profile"].get("embedding_dim") if inc_m else None)
            if ok and inc_dim and summary.get("dim") and summary["dim"] != inc_dim:
                return {"incumbent": incumbent, "recommendation": "REJECT", "failed_checks": ["embedding_dim"],
                        "note": f"dimension {summary['dim']} ≠ incumbent {inc_dim}: swapping would silently break "
                                "every vector index built on this alias. Serve it under a new alias and re-index."}
            return {"incumbent": incumbent, "recommendation": "HOLD" if ok else "REJECT",
                    "note": "embedding benchmarks check latency + semantic sanity only"}
        rep = evaluator.compare(summary, inc_b["summary"] if inc_b else None,
                                float(m["profile"].get("memory_budget_mb") or 0),
                                float(inc_m["profile"].get("memory_budget_mb") or 0) if inc_m else None)
        rep["incumbent"] = incumbent
        rep["alias"] = alias
        # JEV_COMPARE: advisory second opinion over PUBLIC benchmark numbers. Policy (above) decides.
        try:
            d = await self.fabric.evaluate("candidate-vs-incumbent",
                                           {"alias": alias, "candidate": {"model": m["model_id"], **summary},
                                            "incumbent": {"model": inc_m["model_id"] if inc_m else None,
                                                          **(inc_b["summary"] if inc_b else {})},
                                            "policy_deltas": rep.get("delta")}, data_class="PUBLIC")
            rep["jev"] = {"improves": d.decision, "confidence": round(d.confidence, 3), "provider": d.provider,
                          "p_improves": round(d.probabilities.get("yes", 0), 3)}
        except Exception as exc:
            rep["jev"] = {"error": str(exc)[:200]}
        self.reg.event("comparison_complete", mid, recommendation=rep["recommendation"], incumbent=incumbent,
                       jev=rep.get("jev"))
        return rep

    def _compare_web(self, mid: str, summary: dict, incumbent: str | None) -> dict:
        """local/web has its own policy: grounded quality is the point, so the size and latency ratios
        the other aliases use (+20 % memory, +10 % TTFT) would reject every upgrade by design. The
        baseline is whatever answers grounded questions today: the local/web head, else the head of the
        gateway's fallback alias (local/default) measured on the same web suite."""
        base_id = incumbent
        if base_id is None:
            fb = self.reg.alias("local/default")
            base_id = fb["chain"][0] if fb and fb["chain"] else None
        base = self.reg.latest_benchmark(base_id, "web") if base_id else None
        rep = evaluator.compare_web(summary, base["summary"] if base else None)
        rep.update({"alias": "local/web", "incumbent": incumbent, "baseline": base_id,
                    "baseline_benchmarked": base is not None})
        if base_id and base is None:
            rep["note"] = (f"no web-suite benchmark for the baseline {base_id}: run `local-ai models benchmark "
                           f"{base_id} --suite web` first for a measured gain")
        self.reg.event("comparison_complete", mid, recommendation=rep["recommendation"], incumbent=incumbent,
                       baseline=base_id)
        return rep

    # ── operator nomination (a specific model, outside discovery's categories) ──

    async def nominate(self, spec: dict, actor: str, hf=None) -> dict:
        """Register one pinned GGUF from Hugging Face as a CANDIDATE: the same metadata, license and
        hardware-fit gates discovery applies, then the normal download → benchmark → promote path.
        spec: hf_repo, file, category; optional revision (default: current), active_params_b (MoE),
        arch {num_layers, num_kv_heads, head_dim, vocab_size}, chat_template_kwargs, context, concurrency, id."""
        from lif.models import discovery, hf as hfmod
        from lif.models import hardware_fit
        cat = spec.get("category")
        if cat not in CATEGORY_ALIAS:
            raise OpError(f"category must be one of {sorted(CATEGORY_ALIAS)}")
        client = hf or hfmod.HFClient()
        try:
            info = await client.model_info(spec["hf_repo"], spec.get("revision"))
        except Exception as exc:
            raise OpError(f"Hugging Face lookup failed for {spec.get('hf_repo')}: {exc}") from exc
        meta = hfmod.normalize(info, cat)
        files = {f["file"]: f for f in hfmod.gguf_files(info)}
        pick = files.get(spec.get("file") or "")
        if not pick or not pick.get("sha256") or not pick.get("size"):
            raise OpError(f"{spec.get('file')!r} is not a single-file GGUF with a sha256 in {spec['hf_repo']} "
                          f"(have: {sorted(files)[:8]})")
        meta["gguf_pick"] = pick
        if spec.get("active_params_b"):
            meta["active_params_b"] = float(spec["active_params_b"])
        if spec.get("params_b"):
            meta["params_b"] = float(spec["params_b"])
        # Shape from the model card when the repo's config doesn't carry it (GGUF-only repos often don't);
        # without it the estimator assumes a worst-case KV cache.
        for k in ("num_layers", "num_kv_heads", "head_dim", "vocab_size"):
            if isinstance((spec.get("arch") or {}).get(k), int):
                meta[k] = spec["arch"][k]
        if not meta.get("revision") or len(meta["revision"]) != 40:
            raise OpError("could not resolve a 40-char revision")
        why = discovery.metadata_filter({**meta, "downloads": max(meta.get("downloads") or 0, 10**6)}, cat,
                                        {m["model_id"] for m in self.reg.list() if m["blocked"]})
        if why:
            raise OpError(f"rejected by the discovery gates: {why}")
        ctx, conc = int(spec.get("context", 8192)), int(spec.get("concurrency", 2))
        fit = hardware_fit.estimate(meta, device="cpu", context=ctx, concurrency=conc).to_dict()
        if fit["verdict"] != "fits_cpu":
            raise OpError(f"does not fit the CPU tier: {'; '.join(fit.get('reasons') or [])}")
        prof = {"category": cat, "runtime": "llama.cpp", "device": "cpu", "hf_repo": meta["model_id"],
                "revision": meta["revision"], "file": pick["file"], "sha256": pick["sha256"], "size": pick["size"],
                "precision": (pick.get("quant") or "").lower(), "params_b": meta.get("params_b"),
                "context": ctx, "concurrency": conc, "weights_mib": fit["weights_mib"], "anon_mib": fit["anon_mib"],
                "memory_budget_mb": hardware_fit.memory_limit_mib(fit["weights_mib"], fit["anon_mib"]),
                "license": meta.get("license")}
        if meta.get("active_params_b"):
            prof["active_params_b"] = meta["active_params_b"]
        if isinstance(spec.get("chat_template_kwargs"), dict):
            prof["chat_template_kwargs"] = spec["chat_template_kwargs"]
        templates.validate_profile(prof)
        mid = spec.get("id") or discovery.profile_id(meta, "cpu")
        if self.reg.get(mid):
            raise OpError(f"{mid} is already registered ({self.reg.get(mid)['state']})")
        screening = {"nominated_by": actor, "recommendation": {"why": "operator nomination"}, "priority": 0}
        self.reg.upsert(mid, meta["model_id"], meta["revision"], cat, "DISCOVERED", meta=meta, profile=prof, fit=fit,
                        screening=screening, actor=actor, reason="nominated by the operator")
        self.reg.transition(mid, "CANDIDATE", f"operator nomination for {CATEGORY_ALIAS[cat]}", actor)
        return {"id": mid, "state": "CANDIDATE", "alias": CATEGORY_ALIAS[cat], "fit": fit, "profile": prof}

    # ── serving: load / unload / canary / promote / rollback ────────────────

    def _headroom_ok(self, m: dict) -> tuple[bool, str]:
        snap = self.gpu.current()
        need = _resident_mib(m)
        if not snap.reachable:
            return False, "gpusched unreachable: cannot verify memory headroom"
        if snap.mem_available_mib - need < HEADROOM_MIB:
            return False, (f"would leave {snap.mem_available_mib - need:.0f} MiB MemAvailable "
                           f"(< {HEADROOM_MIB} MiB: gpusched headroom + margin)")
        return True, ""

    async def ensure_server(self, mid: str, force: bool = False) -> str:
        """Start (or scale up) the model's server. Starting NEW memory is gated on host
        headroom for every caller (load, canary, promote, rollback) unless forced."""
        m = self._get(mid)
        p = dict(m["profile"])
        name = deployment_name(mid, p)
        dep = await self.k8s.deployment("ai-serving", name) if p.get("endpoint") else None
        running = bool(dep and (dep.get("spec") or {}).get("replicas"))
        if not running and not force:
            ok, why = self._headroom_ok(m)
            if not ok:
                raise OpError(f"cannot start {mid}: {why}")
        if not p.get("endpoint"):
            dep, svc = templates.model_server(name, mid, p, priority="ai-interactive")
            await self.k8s.apply_deployment("ai-serving", dep)
            await self.k8s.apply_service("ai-serving", svc)
            p["endpoint"] = f"http://{name}.ai-serving.svc:8080"
            self.reg.upsert(mid, m["model_id"], m["revision"], m["category"], m["state"], profile=p)
        else:
            await self.k8s.scale("ai-serving", name, 1)
        return name

    async def load(self, mid: str, actor: str) -> dict:
        m = self._get(mid)
        if m["profile"].get("device") != "cpu":
            raise OpError("GPU-tier loads go through gpusched command jobs")
        name = await self.ensure_server(mid, force=actor == "operator-force")
        self.reg.event("model_loaded", mid, actor, deployment=name)
        return {"deployment": name}

    async def unload(self, mid: str, actor: str) -> dict:
        m = self._get(mid)
        users = self.reg.aliases_using(mid)
        if users and actor != "operator-force":
            raise OpError(f"{mid} serves {users}; the alias would fall back. Use force to confirm.")
        name = deployment_name(mid, m["profile"])
        await self.k8s.scale("ai-serving", name, 0)
        self.reg.event("model_unloaded", mid, actor, deployment=name, aliases_affected=users)
        return {"deployment": name, "aliases_affected": users}

    async def canary(self, mid: str, actor: str, alias: str | None = None, percent: float | None = None) -> dict:
        m = self._get(mid)
        if m["state"] not in ("APPROVED", "STANDBY"):
            raise OpError(f"{mid} is {m['state']}; only APPROVED (benchmarked) models enter canary")
        alias = alias or CATEGORY_ALIAS.get(m["category"], "local/default")
        cur = self.reg.alias(alias)
        if not cur or not cur["chain"]:
            raise OpError(f"{alias} has no incumbent; promote directly instead")
        await self.ensure_server(mid)
        pct = float(percent if percent is not None else config.get("models.promotion.canary_percent", 10))
        self.reg.transition(mid, "CANARY", f"{pct:g}% of {alias}", actor)
        return self.reg.set_alias(alias, cur["chain"], actor, note=f"canary {mid}",
                                  canary={"profile": mid, "percent": pct, "since": time.time()})

    async def promote(self, mid: str, actor: str, alias: str | None = None) -> dict:
        m = self._get(mid)
        if m["state"] not in ("CANARY", "APPROVED", "STANDBY"):
            raise OpError(f"{mid} is {m['state']}; never promote an untested model")
        if not self.reg.latest_benchmark(mid):
            raise OpError(f"{mid} has no local benchmark")
        if actor != "operator" and not self.reg.setting("automatic_promotion", config.get("models.automatic_promotion")):
            raise OpError("automatic promotion is disabled")
        if actor != "operator" and self.reg.setting("maintenance", False):
            raise OpError("maintenance mode")
        alias = alias or CATEGORY_ALIAS.get(m["category"], "local/default")
        await self.ensure_server(mid)
        cur = self.reg.alias(alias)
        old = cur["chain"] if cur else []
        chain = [mid] + [p for p in old if p != mid]
        self.reg.transition(mid, "PRODUCTION", f"promoted to {alias}", actor)
        out = self.reg.set_alias(alias, chain, actor, note=f"promote {mid}")
        return out

    async def rollback(self, alias: str, actor: str, to_version: int | None = None) -> dict:
        before = self.reg.alias(alias) or {"chain": [], "canary": None}
        out = self.reg.rollback_alias(alias, actor, to_version)
        for mid in out["chain"]:
            m = self._get(mid)
            await self.ensure_server(mid, force=True)       # restoring known-good capacity is never gated
            if m["state"] != "PRODUCTION":
                self.reg.transition(mid, "PRODUCTION", f"rollback of {alias}", actor, force=True)
        # models dropped by the rollback: STANDBY, and free their controller-managed servers
        dropped = set(before["chain"]) | ({before["canary"]["profile"]} if before.get("canary") else set())
        for mid in dropped - set(out["chain"]):
            if self.reg.aliases_using(mid):
                continue
            m = self._get(mid)
            if m["state"] in ("PRODUCTION", "CANARY"):
                self.reg.transition(mid, "STANDBY", f"rolled back from {alias}", actor, force=True)
            name = deployment_name(mid, m["profile"])
            if name.startswith("lif-"):
                await self.k8s.scale("ai-serving", name, 0)
                self.reg.event("model_unloaded", mid, actor, deployment=name, reason=f"rollback of {alias}")
        return out

    # ── retention / storage ──────────────────────────────────────────────────

    def storage(self) -> dict:
        groups: dict[str, dict[str, Any]] = {}
        crit = self.reg._rollback_critical()
        for m in self.reg.list():
            size = int((m["profile"] or {}).get("size") or (m["fit"] or {}).get("weights_mib", 0) * 2**20) + \
                int(((m["profile"] or {}).get("mmproj") or {}).get("size") or 0)
            g = ("production" if m["state"] in ("PRODUCTION", "CANARY") else
                 "rollback" if m["id"] in crit else
                 "candidate" if m["state"] in ("STAGED", "APPROVED", "STANDBY", "DOWNLOADING", "VALIDATING",
                                               "BENCHMARKING") else
                 "failed" if m["state"] in ("FAILED", "QUARANTINED", "REJECTED", "DEPRECATED") else "metadata_only")
            if g == "metadata_only":
                size = 0
            e = groups.setdefault(g, {"count": 0, "bytes": 0, "models": []})
            e["count"] += 1
            e["bytes"] += size
            e["models"].append(m["id"])
        return groups

    async def gc(self, actor: str = "retention") -> dict:
        """Remove artifacts of REJECTED/FAILED/DEPRECATED models older than 7 days and of
        STAGED candidates idle > 7 days. Registry rows (evidence) are kept."""
        crit = self.reg._rollback_critical()
        now, paths, ids = time.time(), [], []
        for m in self.reg.list(["REJECTED", "FAILED", "DEPRECATED", "STAGED", "QUARANTINED"]):
            if m["pinned"] or m["id"] in crit or self.reg.aliases_using(m["id"]):
                continue
            if now - m["updated"] < 7 * 86400 or not m["profile"].get("file"):
                continue
            if (m["profile"] or {}).get("gc_done"):
                continue
            p = m["profile"]
            paths += [f"{p['hf_repo']}/{p['revision']}/{f}" for f, _ in templates.artifacts(p)]
            ids.append(m["id"])
        if not paths:
            return {"removed": 0}
        name = f"gc-{int(now)}"
        await self.k8s.create_job("ai-serving", templates.gc_job(name, paths))
        for mid in ids:
            m = self._get(mid)
            self.reg.upsert(mid, m["model_id"], m["revision"], m["category"], m["state"],
                            profile={**m["profile"], "gc_done": True})
        self.reg.event("retention_gc", "model-store", actor, job=name, models=ids)
        return {"removed": len(ids), "job": name}

    # ── host memory guard (sheds optional LIF capacity before gpusched's headroom) ──

    def _sustained(self, key: str, cond: bool, now: float, sustain: float) -> bool:
        if not cond:
            self.guard_since.pop(key, None)
            return False
        self.guard_since.setdefault(key, now)
        return now - self.guard_since[key] >= sustain

    async def memory_guard(self) -> None:
        g = config.get("memory_guard") or {}
        snap = self.gpu.current()
        if not snap.reachable or not snap.mem_available_mib or not self.k8s.enabled:
            return                          # no trustworthy reading → change nothing
        # an operator reservation (Settings → reserve_gpu_mib) makes the guard shed earlier
        reserve = float(self.reg.setting("reserve_gpu_mib", 0) or 0)
        avail, now, sustain = snap.mem_available_mib - reserve, time.time(), float(g.get("sustain_sec", 60))
        groups: list[tuple[str, float, float]] = []
        # Large aliases first (local/web): a 12+ GiB server, shed at a higher line so the small tiers stay up,
        # and restored only when its whole limit fits above that line again.
        for alias in g.get("large_aliases") or []:
            a = self.reg.alias(alias)
            head = self.reg.get(a["chain"][0]) if a and a["chain"] else None
            if head and head["profile"].get("endpoint"):
                lo = float(g.get("shed_large_below_mib", 10240))
                budget = float(head["profile"].get("memory_budget_mb") or 0)
                groups.append((deployment_name(head["id"], head["profile"]), lo, lo + budget + 1024))
        for group, lo, hi in (("optional", "shed_optional_below_mib", "restore_optional_above_mib"),
                              ("secondary", "shed_secondary_below_mib", "restore_secondary_above_mib")):
            groups += [(dep, float(g[lo]), float(g[hi])) for dep in g.get(group) or []]
        for dep, lo_mib, hi_mib in groups:
            shed = self.guard_shed.get(dep, False)
            if not shed and self._sustained(f"shed:{dep}", avail < lo_mib, now, sustain):
                await self.k8s.scale("ai-serving", dep, 0)
                self.guard_shed[dep] = True
                self.reg.db.x("INSERT OR REPLACE INTO settings(key,value) VALUES('memory_guard_shed', ?)",
                              (json.dumps(self.guard_shed),))
                self.reg.event("memory_guard_shed", dep, "gpu-resource-manager", mem_available_mib=avail,
                               threshold_mib=lo_mib, reason="protect gpusched headroom for the primary workload")
            elif shed and avail < lo_mib:
                # reconcile: something (an apply, an operator) brought it back while still short
                d = await self.k8s.deployment("ai-serving", dep) or {}
                if (d.get("spec") or {}).get("replicas"):
                    await self.k8s.scale("ai-serving", dep, 0)
                    self.reg.event("memory_guard_reshed", dep, "gpu-resource-manager", mem_available_mib=avail)
            elif shed and self._sustained(f"restore:{dep}", avail > hi_mib, now, sustain):
                await self.k8s.scale("ai-serving", dep, 1)
                self.guard_shed[dep] = False
                self.reg.db.x("INSERT OR REPLACE INTO settings(key,value) VALUES('memory_guard_shed', ?)",
                              (json.dumps(self.guard_shed),))
                self.reg.event("memory_guard_restore", dep, "gpu-resource-manager", mem_available_mib=avail,
                               threshold_mib=hi_mib)

    def guard_status(self) -> dict:
        return {"shed": [d for d, v in self.guard_shed.items() if v], "pending": dict(self.guard_since)}

    # ── Primary-workload protection (state transitions → visible actions) ──────

    async def protect(self) -> None:
        if self.fabric is not None:
            self.fabric.jev_enabled_override = not self.reg.setting("jev_disabled", False)
        await self.memory_guard()
        snap = self.gpu.current()
        prev, self.last_blerbz = self.last_blerbz, snap.state
        if prev is None or prev == snap.state:
            return
        self.reg.event("blerbz_state", snap.state.name, "gpu-resource-manager", frm=prev.name, reason=snap.reason)
        if snap.state == BlerbzState.IMMINENT:
            # CPU tiers yield via the gateway; batch/eval pause themselves; candidate
            # benchmarks abort via should_stop. Record the takeover explicitly.
            running = [k for k, t in self.tasks.items() if k.startswith("benchmark:") and not t.done()]
            self.reg.event("blerbz_takeover", "", "gpu-resource-manager",
                           actions=["gateway: CPU concurrency → 1, max_tokens ≤ 512",
                                    "batch: low-priority work paused", f"benchmarks aborting: {running}"])
        elif prev == BlerbzState.IMMINENT:
            self.reg.event("blerbz_release", "", "gpu-resource-manager",
                           actions=["gateway: normal concurrency", "batch: resumed"])

    def _get(self, mid: str) -> dict:
        m = self.reg.get(mid)
        if m is None:
            raise OpError(f"unknown model {mid}")
        return m


async def embed_benchmark(url: str) -> tuple[dict, dict]:
    """Embedding check: latency/throughput + semantic sanity (paraphrases closer than unrelated)."""
    import math
    import httpx
    pairs = [("The cat sat on the mat.", "A cat is sitting on a mat.", "Interest rates rose by half a point."),
             ("Heavy snow closed the mountain pass.", "The mountain road shut because of snowfall.",
              "The team won the championship game."),
             ("How do I reset my password?", "Steps to change a forgotten password", "Best pizza toppings")]

    def cos(a, b):
        return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))
    res, lat, errors, passed = {}, [], 0, 0
    async with httpx.AsyncClient(timeout=60) as c:
        for i, (a, b, z) in enumerate(pairs):
            t = time.perf_counter()
            try:
                r = await c.post(f"{url}/v1/embeddings", json={"input": [a, b, z]})
                r.raise_for_status()
                e = [d["embedding"] for d in r.json()["data"]]
                lat.append(time.perf_counter() - t)
                ok = cos(e[0], e[1]) > cos(e[0], e[2])
                passed += ok
                res[f"pair-{i}"] = {"pass": ok, "sim_para": round(cos(e[0], e[1]), 3), "sim_unrel": round(cos(e[0], e[2]), 3)}
            except Exception as exc:
                errors += 1
                res[f"pair-{i}"] = {"pass": False, "error": str(exc)[:200]}
        t = time.perf_counter()
        batch = [f"sentence number {i} about local inference" for i in range(64)]
        r = await c.post(f"{url}/v1/embeddings", json={"input": batch})
        wall = time.perf_counter() - t
    return res, {"sanity_pass": passed == len(pairs), "latency_ms_p50": round(sorted(lat)[len(lat) // 2] * 1000, 1)
                 if lat else None, "embeddings_per_sec": round(64 / wall, 1) if r.status_code == 200 else None,
                 "errors": errors, "dim": len(e[0]) if errors < len(pairs) else None}
