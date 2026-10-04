"""local/web promotion path: MoE sizing, operator nomination, the web suite and its policy, promotion, and
the memory guard shedding a large local/web server first."""
from __future__ import annotations

import time

import pytest

from lif.gpu.state import BlerbzState, Snapshot
from lif.models import evaluator, hardware_fit
from lif.models.registry import Registry

SHA = "ef9b12f2ff56c69cf32153a02784e7a3c88bf524"
GGUF_SHA = "27cd6c432c7672cb812a92f611cf3ba7bbc35928262bb1e1253ff4ee6ae35901"
SIZE = 12_109_566_624
ARCH = {"num_layers": 24, "num_kv_heads": 8, "head_dim": 64, "vocab_size": 201_088}     # gpt-oss-20b model card


def gpt_oss_info(**over) -> dict:
    return {"id": "ggml-org/gpt-oss-20b-GGUF", "sha": SHA, "pipeline_tag": None, "downloads": 130_000,
            "cardData": {"license": "apache-2.0"}, "tags": ["gguf"], "gguf": {"total": 20_914_757_184},
            "config": {}, "siblings": [
                {"rfilename": "gpt-oss-20b-MXFP4.gguf", "size": SIZE, "lfs": {"sha256": GGUF_SHA}},
                {"rfilename": "eagle3-gpt-oss-20b-Q8_0.gguf", "size": 921_488_000, "lfs": {"sha256": "e" * 64}}],
            **over}


class FakeHF:
    def __init__(self, info):
        self.info, self.asked = info, []

    async def model_info(self, model_id, revision=None):
        self.asked.append((model_id, revision))
        return self.info


class FakeK8s:
    enabled = True

    def __init__(self):
        self.deps, self.scaled = {}, []

    async def deployment(self, ns, name):
        return self.deps.get(name)

    async def apply_deployment(self, ns, body):
        self.deps[body["metadata"]["name"]] = {"spec": {"replicas": 1}, "body": body}

    async def apply_service(self, ns, body):
        return {}

    async def scale(self, ns, name, n):
        self.scaled.append((name, n))
        self.deps.setdefault(name, {"spec": {}})["spec"]["replicas"] = n


class Gpu:
    def __init__(self, mem):
        self.snap = Snapshot(reachable=True, state=BlerbzState.LOW, mem_available_mib=mem, ts=time.time())

    def current(self):
        return self.snap


def life_with(tmp_path, mem=40_000):
    from lif.controller.lifecycle import Lifecycle
    reg = Registry(str(tmp_path / "r.db"))
    k8s, gpu = FakeK8s(), Gpu(mem)
    return Lifecycle(reg, k8s, gpu, None), reg, k8s, gpu


# ── sizing ─────────────────────────────────────────────────────────────────────────────────────

def test_moe_is_sized_by_active_parameters():
    meta = {"params_b": 20.9, "active_params_b": 3.6, "gguf_pick": {"size": SIZE, "quant": "MXFP4"},
            "num_layers": 24, "num_kv_heads": 8, "head_dim": 64, "vocab_size": 201_088}
    r = hardware_fit.estimate(meta, device="cpu", context=8192, concurrency=1)
    assert r.verdict == "fits_cpu" and 20 < r.est_cpu_decode_tps < 35
    dense = hardware_fit.estimate({**meta, "active_params_b": None}, device="cpu", context=8192, concurrency=1)
    assert dense.verdict == "no_fit"                         # the same bytes read per token would be too slow


def test_resident_ceiling_still_applies_to_moe():
    meta = {"params_b": 35, "active_params_b": 3, "gguf_pick": {"size": 21 * 2**30, "quant": "Q4_K_M"},
            "num_layers": 40, "num_kv_heads": 4, "head_dim": 128}
    r = hardware_fit.estimate(meta, device="cpu")
    assert r.verdict != "fits_cpu" and any("resident" in x for x in r.reasons)


# ── nomination ─────────────────────────────────────────────────────────────────────────────────

async def test_nominate_registers_a_pinned_candidate(tmp_path):
    life, reg, *_ = life_with(tmp_path)
    hf = FakeHF(gpt_oss_info())
    out = await life.nominate({"hf_repo": "ggml-org/gpt-oss-20b-GGUF", "file": "gpt-oss-20b-MXFP4.gguf",
                               "category": "web", "revision": SHA, "active_params_b": 3.6, "concurrency": 1, "arch": ARCH,
                               "chat_template_kwargs": {"reasoning_effort": "low"}}, "operator", hf=hf)
    m = reg.get(out["id"])
    assert out["alias"] == "local/web" and m["state"] == "CANDIDATE" and m["category"] == "web"
    p = m["profile"]
    assert p["sha256"] == GGUF_SHA and p["revision"] == SHA and p["active_params_b"] == 3.6
    assert p["chat_template_kwargs"] == {"reasoning_effort": "low"}
    assert p["memory_budget_mb"] >= p["weights_mib"] + p["anon_mib"]       # never below weights + anon
    assert hf.asked == [("ggml-org/gpt-oss-20b-GGUF", SHA)]


@pytest.mark.parametrize("spec,why", [
    ({"file": "missing.gguf", "category": "web"}, "single-file GGUF"),
    ({"file": "gpt-oss-20b-MXFP4.gguf", "category": "chat"}, "category must be"),
    ({"file": "gpt-oss-20b-MXFP4.gguf", "category": "web", "active_params_b": None, "arch": ARCH}, "does not fit"),
])
async def test_nominate_refuses(tmp_path, spec, why):
    from lif.controller.lifecycle import OpError
    life, *_ = life_with(tmp_path)
    with pytest.raises(OpError, match=why):
        await life.nominate({"hf_repo": "ggml-org/gpt-oss-20b-GGUF", **spec}, "operator", hf=FakeHF(gpt_oss_info()))


async def test_nominate_applies_the_license_gate(tmp_path):
    from lif.controller.lifecycle import OpError
    life, *_ = life_with(tmp_path)
    with pytest.raises(OpError, match="license"):
        await life.nominate({"hf_repo": "ggml-org/gpt-oss-20b-GGUF", "file": "gpt-oss-20b-MXFP4.gguf",
                             "category": "web", "active_params_b": 3.6},
                            "operator", hf=FakeHF(gpt_oss_info(cardData={"license": "cc-by-nc-4.0"})))


# ── the web suite and its policy ───────────────────────────────────────────────────────────────

def test_web_suite_items_build_production_prompts():
    s = evaluator.load_suite("web")
    assert s["suite"] == "web" and len(s["items"]) >= 12
    cats = {i["cat"] for i in s["items"]}
    assert {"honesty", "safety", "summarization"} <= cats
    msgs = evaluator.item_messages(s["items"][0])
    assert msgs[0]["role"] == "system" and msgs[0]["content"].startswith("Today is Saturday, October 3, 2026")
    assert "<web_results>" in msgs[-1]["content"] and msgs[-1]["content"].endswith(s["items"][0]["prompt"])
    empty = next(i for i in s["items"] if i["id"] == "web-empty-01")
    assert "<web_results>" in evaluator.item_messages(empty)[-1]["content"]


def test_grounded_checker():
    item = {"checker": "grounded", "expect": ["(?i)seahawks"], "forbid": ["(?i)pineapple"]}
    assert evaluator.check(item, "The Seattle Seahawks won Super Bowl LX, 29-13 [1].")[0]
    assert not evaluator.check(item, "The Seattle Seahawks won.")[0]                       # no citation
    assert not evaluator.check(item, "This may be hypothetical, but the Seahawks won [1].")[0]
    assert not evaluator.check(item, "PINEAPPLE. Seahawks [1].")[0]
    assert evaluator.check({**item, "cite": False, "expect": ["(?i)couldn'?t"]}, "I couldn't confirm that.")[0]


BASE = {"quality": 0.73, "latency_ms_p50": 4200, "errors": 0, "by_category": {"honesty": 1.0, "safety": 1.0}}


def test_compare_web_policy():
    good = {"quality": 0.93, "latency_ms_p50": 9000, "errors": 0, "by_category": {"honesty": 1.0, "safety": 1.0}}
    r = evaluator.compare_web(good, BASE)
    assert r["recommendation"] == "HOLD" and r["delta"]["quality"] == 0.2      # HOLD → APPROVED, never auto-canary
    assert evaluator.compare_web({**good, "quality": 0.76}, BASE)["failed_checks"] == ["quality_floor", "gain"]
    assert "latency" in evaluator.compare_web({**good, "latency_ms_p50": 30_000}, BASE)["failed_checks"]
    assert "safety" in evaluator.compare_web({**good, "by_category": {"safety": 0.0}}, BASE)["failed_checks"]
    assert evaluator.compare_web(good, None)["recommendation"] == "HOLD"     # no baseline: absolute checks only


async def test_compare_uses_local_default_as_baseline(tmp_path):
    life, reg, *_ = life_with(tmp_path)
    reg.upsert("inc4b", "unsloth/Qwen3-4B", "a" * 40, "general", "PRODUCTION", profile={"endpoint": "http://tier0"})
    reg.set_alias("local/default", ["inc4b"], "test")
    reg.add_benchmark("inc4b", "web", {}, BASE)
    reg.upsert("oss", "ggml-org/gpt-oss-20b-GGUF", SHA, "web", "BENCHMARKING", profile={})
    rep = await life.compare("oss", {"quality": 0.93, "latency_ms_p50": 9000, "errors": 0, "by_category": {}})
    assert rep["policy"] == "web" and rep["baseline"] == "inc4b" and rep["recommendation"] == "HOLD"
    reg.upsert("inc_nobench", "x/y", "b" * 40, "general", "PRODUCTION", profile={"endpoint": "http://x"})
    reg.set_alias("local/default", ["inc_nobench"], "test")
    rep = await life.compare("oss", {"quality": 0.93, "latency_ms_p50": 9000, "errors": 0, "by_category": {}})
    assert "gain" not in rep["checks"] and "--suite web" in rep["note"]


def test_benchmark_suite_override_rules(tmp_path):
    from lif.controller.lifecycle import OpError
    life, reg, *_ = life_with(tmp_path)
    reg.upsert("staged", "a/b", "c" * 40, "general", "STAGED", profile={"device": "cpu"})
    with pytest.raises(OpError, match="unknown suite"):
        life.benchmark("staged", "operator", suite="nope")
    with pytest.raises(OpError, match="only on a live model"):
        life.benchmark("staged", "operator", suite="web")


# ── promote, headroom and the memory guard ─────────────────────────────────────────────────────

async def _nominated_and_approved(life, reg) -> str:
    out = await life.nominate({"hf_repo": "ggml-org/gpt-oss-20b-GGUF", "file": "gpt-oss-20b-MXFP4.gguf",
                               "category": "web", "active_params_b": 3.6, "concurrency": 1, "arch": ARCH},
                              "operator", hf=FakeHF(gpt_oss_info()))
    mid = out["id"]
    for st in ("DOWNLOADING", "STAGED", "BENCHMARKING", "APPROVED"):
        reg.transition(mid, st, "test", "test", force=True)
    reg.add_benchmark(mid, "web", {}, {"quality": 0.93})
    return mid


async def test_large_model_start_counts_its_weights(tmp_path):
    from lif.controller.lifecycle import OpError
    life, reg, k8s, gpu = life_with(tmp_path, mem=20_000)    # all residents loaded: ~20 GiB free
    mid = await _nominated_and_approved(life, reg)
    with pytest.raises(OpError, match="cannot start"):
        await life.promote(mid, "operator", "local/web")     # 20 GiB − ~12.9 GiB < 9 GiB headroom
    gpu.snap = Snapshot(reachable=True, state=BlerbzState.LOW, mem_available_mib=40_000, ts=time.time())
    out = await life.promote(mid, "operator", "local/web")
    assert out["chain"] == [mid] and reg.get(mid)["state"] == "PRODUCTION"
    assert life.routing_table()["aliases"]["local/web"] == [mid]


async def test_memory_guard_sheds_local_web_first_and_restores_with_room(tmp_path, monkeypatch):
    life, reg, k8s, gpu = life_with(tmp_path, mem=40_000)
    mid = await _nominated_and_approved(life, reg)
    await life.promote(mid, "operator", "local/web")
    dep = next(n for n in k8s.deps if n.startswith("lif-"))
    budget = reg.get(mid)["profile"]["memory_budget_mb"]
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    async def tick(mem):
        gpu.snap = Snapshot(reachable=True, state=BlerbzState.LOW, mem_available_mib=mem, ts=clock[0])
        await life.memory_guard()
        clock[0] += 61
        await life.memory_guard()
    await tick(9_800)                       # below the large line (10 GiB), above tier0-small's (9 GiB)
    assert (dep, 0) in k8s.scaled and ("tier0-small", 0) not in k8s.scaled
    await tick(10_240 + budget)             # not yet room for its whole limit above the line
    assert (dep, 1) not in k8s.scaled
    await tick(10_240 + budget + 2_000)
    assert k8s.scaled[-1] == (dep, 1)


async def test_evaluator_retries_a_dropped_keepalive_connection(monkeypatch):
    import httpx as _httpx
    calls = []

    async def flaky(client, url, model, prompt, max_tokens, extra, messages=None):
        calls.append(1)
        if len(calls) == 1:
            raise _httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return {"text": "The Seattle Seahawks won [1].", "ttft": 0.1, "total": 0.2, "decode_tps": 20.0, "usage": {}}
    monkeypatch.setattr(evaluator, "_one", flaky)
    out = await evaluator._ask(None, "http://x", "m", {"prompt": "q", "grounded": {"now": "2026-10-03T09:00",
                                                                                 "evidence": []}}, 64, {})
    assert out["text"].startswith("The Seattle") and len(calls) == 2
