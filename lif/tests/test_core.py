"""Core unit tests: policy, Decision Fabric, DAG, primary-workload state, hardware fit, registry, router."""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from lif.decision.dag import DagRuntime, Workflow
from lif.decision.fabric import DecisionFabric
from lif.decision.providers import JevProvider
from lif.decision.rules import rules
from lif.decision.types import load_definitions
from lif.gpu.state import (BlerbzState, GpuStateWatcher, Job, Snapshot, Verdict, can_run, derive_state,
                          snapshot_from_metrics)
from lif.models import hardware_fit
from lif.models.registry import Registry, RegistryError
from lif.policy import engine as policy
from lif.routing.router import NoRoute, Router

SHA_A, SHA_B = "a" * 40, "b" * 40


# ── policy ───────────────────────────────────────────────────────────────────

def test_detectors_only_raise_class():
    c = policy.classify("contact me at jane@example.com", declared="PUBLIC")
    assert c.data_class == policy.DataClass.CONFIDENTIAL and "email" in c.findings
    c = policy.classify("key sk_live_ABCDEFGHIJKLMNOPQRSTU", declared="PUBLIC")
    assert c.data_class == policy.DataClass.RESTRICTED
    c = policy.classify("hello", declared="RESTRICTED")
    assert c.data_class == policy.DataClass.RESTRICTED       # never lowered
    assert policy.classify("hello").data_class == policy.DataClass.CONFIDENTIAL   # default class


def test_may_send():
    assert policy.may_send(policy.DataClass.PUBLIC, "jev")
    assert not policy.may_send(policy.DataClass.INTERNAL, "jev")       # not in jev_allowed by default
    assert not policy.may_send(policy.DataClass.CONFIDENTIAL, "jev")
    assert not policy.may_send(policy.DataClass.RESTRICTED, "jev")
    assert not policy.may_send(policy.DataClass.PUBLIC, "external_llm")


def test_gate_thresholds():
    assert policy.gate(0.99) == policy.Gate.AUTO
    assert policy.gate(0.9) == policy.Gate.VALIDATE
    assert policy.gate(0.75) == policy.Gate.LOCAL_LLM
    assert policy.gate(0.5) == policy.Gate.ESCALATE
    assert policy.gate(0.9, {"auto": 0.85}) == policy.Gate.AUTO


# ── decision fabric with a mocked Jev ─────────────────────────────────────────

def _jev(handler) -> JevProvider:
    p = JevProvider("jv_test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    p.enabled = True
    return p


def _answer(req: httpx.Request) -> httpx.Response:
    body = json.loads(req.content)
    ans = {}
    for k, q in body["questions"].items():
        if q["type"] == "choice":
            first = list(q["criteria"])[0]
            ans[k] = {"type": "choice", "choice": first, "confidence": 0.99, "probabilities": {first: 0.99}}
        elif q["type"] == "score":
            ans[k] = {"type": "score", "score": 1.0, "confidence": 0.95, "probabilities": {"1": 0.95}}
        else:
            ans[k] = {"type": "noul", "noul": 0.9}
    return httpx.Response(200, json={"model": "jev-1.13.0", "answers": ans, "usage": {"input_tokens": 100}})


async def test_group_is_one_jev_call_and_cached():
    calls = []

    def h(req):
        calls.append(req)
        return _answer(req)
    fab = DecisionFabric(load_definitions(), rules, jev=_jev(h))
    st = {"model_id": "Qwen/X", "downloads": 10}
    out = await fab.evaluate_group(["model-suitability", "operational-risk"], st, data_class="PUBLIC")
    assert len(calls) == 1 and set(out) == {"model-suitability", "operational-risk"}
    assert out["model-suitability"].provider == "jev" and out["model-suitability"].action == policy.Gate.AUTO
    again = await fab.evaluate("model-suitability", st, data_class="PUBLIC")
    assert again.cached and len(calls) == 1


async def test_private_data_never_reaches_jev():
    calls = []
    fab = DecisionFabric(load_definitions(), rules, jev=_jev(lambda r: (calls.append(r), _answer(r))[1]))
    r = await fab.evaluate("request-route", {"last_user": "fix my python bug"})          # CONFIDENTIAL default
    assert r.provider == "rules" and not calls
    r = await fab.evaluate("request-route", {"last_user": "password: hunter2xyz fix"}, data_class="PUBLIC")
    assert r.provider == "rules" and not calls                                           # detector → RESTRICTED


async def test_jev_outage_falls_back_and_breaker_opens():
    fab = DecisionFabric(load_definitions(), rules, jev=_jev(lambda r: httpx.Response(502)))
    for _ in range(4):
        r = await fab.evaluate("batch-priority", {"deadline_hours": 1, "n": _}, data_class="PUBLIC")
        assert r.provider == "rules" and r.decision == "urgent"
    assert fab.jev.breaker.fails >= 3


async def test_default_when_everything_fails_forces_escalate():
    fab = DecisionFabric(load_definitions(), rules, jev=None)
    r = await fab.evaluate("candidate-vs-incumbent", {"x": 1})      # no rules, no jev
    assert r.provider == "default" and r.action == policy.Gate.ESCALATE and r.decision == "no"


# ── DAG ──────────────────────────────────────────────────────────────────────

async def test_dag_runs_independent_nodes_concurrently():
    fab = DecisionFabric(load_definitions(), rules, jev=None)
    rt = DagRuntime(fab)

    async def slow(ctx):
        await asyncio.sleep(0.3)
        return 1
    for n in ("b", "c", "d"):
        rt.register(n)(slow)
    rt.register("a")(lambda ctx: 0)
    rt.register("agg")(lambda ctx: ctx["b"] + ctx["c"] + ctx["d"])
    wf = Workflow.from_dict({"name": "t", "nodes": {
        "a": {"type": "deterministic", "fn": "a"},
        "b": {"type": "deterministic", "fn": "b", "depends_on": ["a"]},
        "c": {"type": "deterministic", "fn": "c", "depends_on": ["a"]},
        "d": {"type": "deterministic", "fn": "d", "depends_on": ["a"]},
        "agg": {"type": "policy", "fn": "agg", "depends_on": ["b", "c", "d"]}}})
    t0 = time.perf_counter()
    run = await rt.run(wf, {})
    assert run.results["agg"] == 3
    assert time.perf_counter() - t0 < 0.7             # parallel (~0.3 s), not sequential (0.9 s)
    assert run.waves[1] == ["b", "c", "d"]


def test_dag_rejects_cycles():
    with pytest.raises(ValueError):
        Workflow.from_dict({"name": "c", "nodes": {"a": {"type": "deterministic", "fn": "x", "depends_on": ["b"]},
                                                   "b": {"type": "deterministic", "fn": "x", "depends_on": ["a"]}}})


async def test_candidate_workflow_groups_jev_calls():
    calls = []
    fab = DecisionFabric(load_definitions(), rules, jev=_jev(lambda r: (calls.append(r), _answer(r))[1]))
    rt = DagRuntime(fab)
    rt.load()
    from lif.models.discovery import build_dag
    build_dag(rt, {})
    run = await rt.run("candidate-model-analysis", {"model_id": "Qwen/Qwen3-4B", "category": "fast", "license": "apache-2.0",
                                                     "hardware_fit": "fits_cpu", "downloads": 100000})
    assert run.jev_calls == 2 and len(calls) == 2      # suitability+risk together, improvement separately
    assert run.results["recommendation"]["action"] in ("shortlist", "review", "reject", "hold")


# ── Primary-workload state / CAN_RUN ─────────────────────────────────────────

METRICS = """
gpusched_up 1
gpusched_leases{klass="production",state="active"} 1
gpusched_forecast_p_arrival_next_hour 0.05
gpusched_capacity_admissible_mib 1200
gpusched_resident_loaded{resident="image"} 1
"""


def test_state_from_metrics():
    s = snapshot_from_metrics(METRICS)
    assert s.production_live and s.state == BlerbzState.IMMINENT
    s = snapshot_from_metrics(METRICS.replace('klass="production"', 'klass="background"'))
    assert s.state == BlerbzState.LOW
    s = snapshot_from_metrics(METRICS.replace("} 1\ngpusched_forecast", "} 0\ngpusched_forecast")
                              .replace("0.05", "0.6"))
    assert s.state == BlerbzState.HIGH
    s = snapshot_from_metrics(METRICS.replace('resident="image"} 1', 'resident="image"} 0')
                              .replace('klass="production"', 'klass="x"'))
    assert s.state == BlerbzState.IMMINENT                       # reloading resident


def test_unreachable_is_failsafe():
    assert derive_state(Snapshot(reachable=False))[0] == BlerbzState.IMMINENT


def test_watcher_stays_quiet_when_primary_workload_is_off():
    """A fresh install does not fail-safe to IMMINENT just because gpusched is absent."""
    snap = GpuStateWatcher().current()
    assert snap.state == BlerbzState.LOW and snap.reachable and "standalone" in snap.reason


def test_can_run():
    imminent = Snapshot(reachable=True, production_live=True, state=BlerbzState.IMMINENT, admissible_mib=1000)
    low = Snapshot(reachable=True, state=BlerbzState.LOW, admissible_mib=1000)
    assert can_run(Job(0), imminent)[0] == Verdict.RUN
    assert can_run(Job(5), imminent)[0] == Verdict.PAUSE_BACKGROUND
    assert can_run(Job(2), imminent)[0] == Verdict.RUN_DEGRADED
    assert can_run(Job(5), low)[0] == Verdict.RUN
    assert can_run(Job(5, device="gpu", mem_mb=20000), low)[0] == Verdict.QUEUE


# ── hardware fit ─────────────────────────────────────────────────────────────

def test_hardware_fit_measured_budgets():
    small = {"params_b": 4.0, "gguf_pick": {"size": 2_497_281_120}, "num_layers": 36, "num_kv_heads": 8, "head_dim": 128}
    r = hardware_fit.estimate(small, admissible_mib=1200)
    assert r.verdict == "fits_cpu" and 15 < r.est_cpu_decode_tps < 30
    big = {"params_b": 32, "precision": "BF16"}
    assert hardware_fit.estimate(big, admissible_mib=1200).verdict == "no_fit"   # 61 GB + reserve > 35 GB
    mid = {"params_b": 14, "gguf_pick": {"size": 9_000_000_000}}
    assert hardware_fit.estimate(mid, admissible_mib=1200).verdict == "fits_when_gramz_unloaded"
    assert hardware_fit.estimate({"model_id": "x"}).verdict == "no_fit"          # unknown size → never guess


# ── registry ─────────────────────────────────────────────────────────────────

def _reg(tmp_path) -> Registry:
    r = Registry(str(tmp_path / "reg.db"))
    r.upsert("m1", "org/m1", SHA_A, "fast", "PRODUCTION", profile={"endpoint": "http://m1:8080"})
    r.upsert("m2", "org/m2", SHA_B, "fast", "APPROVED", profile={"endpoint": "http://m2:8080"})
    return r


def test_registry_rejects_mutable_revision(tmp_path):
    r = Registry(str(tmp_path / "r.db"))
    with pytest.raises(RegistryError):
        r.upsert("x", "org/x", "main", "fast", "DISCOVERED")


def test_registry_transitions_and_protection(tmp_path):
    r = _reg(tmp_path)
    with pytest.raises(RegistryError):
        r.transition("m2", "DOWNLOADING", "nope")                     # APPROVED → DOWNLOADING not allowed
    r.set_alias("local/fast", ["m1"], "test")
    with pytest.raises(RegistryError):
        r.delete("m1")                                                # serving
    with pytest.raises(RegistryError):
        r.transition("m1", "DEPRECATED", "x")                         # still referenced by alias


def test_alias_rollback(tmp_path):
    r = _reg(tmp_path)
    r.set_alias("local/fast", ["m1"], "test")
    r.set_alias("local/fast", ["m2", "m1"], "test", note="promote m2")
    assert r.alias("local/fast")["chain"] == ["m2", "m1"]
    out = r.rollback_alias("local/fast", "test")
    assert out["chain"] == ["m1"] and out["version"] == 3
    assert any(e["kind"] == "alias_rollback" for e in r.activity())


def test_alias_refuses_untested(tmp_path):
    r = _reg(tmp_path)
    r.upsert("m3", "org/m3", "c" * 40, "fast", "STAGED")
    with pytest.raises(RegistryError):
        r.set_alias("local/fast", ["m3"], "test")


# ── router ───────────────────────────────────────────────────────────────────

def _router() -> Router:
    rt = Router()
    rt._apply({"big": {"endpoint": "http://big:8080", "params_b": 4, "category": "general"},
               "small": {"endpoint": "http://small:8080", "params_b": 1.7, "category": "fast"},
               "can": {"endpoint": "http://can:8080", "params_b": 4, "category": "general"}},
              {"local/default": ["big", "small"], "local/vision": []}, {"local/default": 4}, "test",
              {"local/default": {"profile": "can", "percent": 100}})
    for n in rt.health:
        rt.health[n].ok = True
    return rt


def test_router_fallback_is_explicit():
    rt = _router()
    rt.canaries = {}
    assert rt.resolve("local/default").profile.name == "big"
    rt.health["big"].ok = False
    r = rt.resolve("local/default")
    assert r.profile.name == "small" and r.fallback and r.degraded and r.meta()["fallback"]
    with pytest.raises(NoRoute):
        rt.resolve("local/vision")
    with pytest.raises(NoRoute):
        rt.resolve("local/nope")


def test_router_canary():
    rt = _router()
    r = rt.resolve("local/default")
    assert r.profile.name == "can" and r.canary and r.meta()["canary"]
    rt.health["can"].ok = False
    assert rt.resolve("local/default").profile.name == "big"     # unhealthy canary never served


# ── memory guard ─────────────────────────────────────────────────────────────

async def test_memory_guard_hysteresis(tmp_path, monkeypatch):
    from lif.controller.lifecycle import Lifecycle
    scaled = []

    class FakeK8s:
        enabled = True

        async def scale(self, ns, name, n):
            scaled.append((name, n))

        async def deployment(self, ns, name):
            return {"spec": {"replicas": 0}}

    class FakeGpu:
        snap = Snapshot(reachable=True, state=BlerbzState.LOW, mem_available_mib=8000, ts=time.time())

        def current(self):
            return self.snap
    gpu = FakeGpu()
    life = Lifecycle(Registry(str(tmp_path / "r.db")), FakeK8s(), gpu, None)
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    await life.memory_guard()                     # below 9 GiB but not yet sustained
    assert scaled == []
    clock[0] += 61
    await life.memory_guard()
    assert scaled == [("tier0-small", 0)]         # optional shed; embedding kept (8000 > 6144)
    gpu.snap = Snapshot(reachable=True, state=BlerbzState.LOW, mem_available_mib=9800, ts=clock[0])
    clock[0] += 120
    await life.memory_guard()
    assert scaled == [("tier0-small", 0)]         # 9.8 GiB: inside the hysteresis band (9216–13056), no flapping
    gpu.snap = Snapshot(reachable=True, state=BlerbzState.LOW, mem_available_mib=13500, ts=clock[0])
    await life.memory_guard()
    clock[0] += 61
    await life.memory_guard()
    assert scaled[-1] == ("tier0-small", 1)
    gpu.snap = Snapshot(reachable=False)
    await life.memory_guard()                     # unreachable → no action
    assert len(scaled) == 2


async def test_discovery_reports_hf_outage(tmp_path):
    from lif.models.discovery import Discovery

    class DeadHF:
        async def search(self, **kw):
            raise httpx.ConnectError("no route to host")
    fab = DecisionFabric(load_definitions(), rules, jev=None)
    rt = DagRuntime(fab)
    rt.load()
    reg = Registry(str(tmp_path / "r.db"))
    out = await Discovery(reg, rt, hfc=DeadHF()).run(["fast", "embedding"])
    assert out["status"] == "failed" and "unreachable" in out["error"]
    assert reg.discovery_runs()[0]["status"] == "failed"


# ── lifecycle: canary → promote → rollback (acceptance G/H) ──────────────────

async def test_lifecycle_canary_promote_rollback(tmp_path):
    from lif.controller.lifecycle import Lifecycle, OpError

    class FakeK8s:
        enabled = True

        def __init__(self):
            self.deps, self.scaled = {}, []

        async def deployment(self, ns, name):
            return self.deps.get(name)

        async def apply_deployment(self, ns, body):
            self.deps[body["metadata"]["name"]] = {"spec": {"replicas": 1}}

        async def apply_service(self, ns, body):
            return {}

        async def scale(self, ns, name, n):
            self.scaled.append((name, n))
            self.deps.setdefault(name, {"spec": {}})["spec"]["replicas"] = n

    class Gpu:
        snap = Snapshot(reachable=True, state=BlerbzState.LOW, mem_available_mib=20000, ts=time.time())

        def current(self):
            return self.snap

    reg = Registry(str(tmp_path / "r.db"))
    reg.upsert("inc", "Qwen/inc", SHA_A, "coding", "PRODUCTION",
               profile={"endpoint": "http://tier0.ai-serving.svc:8080", "params_b": 4})
    reg.set_alias("local/code", ["inc"], "test")
    cand_profile = {"hf_repo": "Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF", "revision": SHA_B,
                    "file": "qwen2.5-coder-1.5b-instruct-q4_k_m.gguf", "sha256": "c" * 64,
                    "category": "coding", "params_b": 1.5, "memory_budget_mb": 2048}
    reg.upsert("cand", "Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF", SHA_B, "coding", "APPROVED",
               profile=cand_profile, fit={"anon_mib": 500})
    reg.add_benchmark("cand", "core", {}, {"quality": 0.9})
    k8s, gpu = FakeK8s(), Gpu()
    life = Lifecycle(reg, k8s, gpu, None)

    gpu.snap = Snapshot(reachable=True, state=BlerbzState.LOW, mem_available_mib=9000, ts=time.time())
    with pytest.raises(OpError, match="cannot start"):
        await life.canary("cand", "operator", "local/code", 10)          # headroom gate
    assert reg.get("cand")["state"] == "APPROVED"

    gpu.snap = Snapshot(reachable=True, state=BlerbzState.LOW, mem_available_mib=20000, ts=time.time())
    a = await life.canary("cand", "operator", "local/code", 10)
    assert a["canary"]["profile"] == "cand" and reg.get("cand")["state"] == "CANARY"
    assert life.routing_table()["canaries"]["local/code"]["percent"] == 10

    a = await life.promote("cand", "operator", "local/code")
    assert a["chain"] == ["cand", "inc"] and reg.get("cand")["state"] == "PRODUCTION"

    a = await life.rollback("local/code", "operator")
    assert a["chain"] == ["inc"] and a["canary"] is None
    assert reg.get("cand")["state"] == "STANDBY"
    assert any(n.startswith("lif-") and r == 0 for n, r in k8s.scaled)   # its server was freed
    kinds = [e["kind"] for e in reg.activity()]
    assert "alias_rollback" in kinds and kinds.count("alias_changed") >= 4


def test_rollback_during_canary_drops_canary(tmp_path):
    r = _reg(tmp_path)
    r.set_alias("local/fast", ["m1"], "test")
    r.set_alias("local/fast", ["m1"], "test", canary={"profile": "m2", "percent": 10})
    out = r.rollback_alias("local/fast", "test")
    assert out["chain"] == ["m1"] and out["canary"] is None


def test_shared_gpu_engine_yields_to_blerbz():
    rt = Router()
    rt._apply({"gpu32": {"endpoint": "http://10.42.0.1:18102", "params_b": 32, "category": "general",
                         "device": "gpu-shared", "yield_on_blerbz": True, "auth_secret": "LIF_ENGINE_KEY"},
               "cpu4": {"endpoint": "http://tier0:8080", "params_b": 4, "category": "general"}},
              {"local/default": ["gpu32", "cpu4"]}, {"local/default": 4}, "test")
    for n in rt.health:
        rt.health[n].ok = True
    assert rt.resolve("local/default").profile.name == "gpu32"
    r = rt.resolve("local/default", blerbz_imminent=True)
    assert r.profile.name == "cpu4" and r.fallback and "primary-workload production" in r.reason
    assert rt.alias_status(blerbz_imminent=True)["local/default"]["served_by"] == "cpu4"


# ── SQLite backup script (scripts/sqlite_backup.py) ────────────────────────────

def _backup_mod():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("sqlite_backup", Path(__file__).parents[1] / "scripts" / "sqlite_backup.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_sqlite_backup_sigv4_matches_aws_example():
    import datetime as dt
    b = _backup_mod()
    h = b.sigv4_headers("GET", "https://examplebucket.s3.amazonaws.com/test.txt", "AKIAIOSFODNN7EXAMPLE",
                        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1", b.EMPTY_SHA,
                        now=dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc), extra={"Range": "bytes=0-9"})
    assert h["authorization"].endswith("f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


def test_sqlite_backup_round_trip_with_wal(tmp_path):
    """Backup while a WAL writer has uncommitted-to-main-file data; restore verifies sha + integrity."""
    import sqlite3
    b = _backup_mod()
    src = tmp_path / "data"
    src.mkdir()
    con = sqlite3.connect(src / "decisions.db")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t(x)")
    con.executemany("INSERT INTO t VALUES(?)", [(i,) for i in range(500)])
    con.commit()                                       # rows live in the -wal file, connection still open

    class FakeS3:
        def __init__(self):
            self.objs = {"lif-sqlite/2000-01-01/old.db.gz": b"x"}
        def put(self, k, v): self.objs[k] = v
        def get(self, k): return self.objs[k]
        def delete(self, k): self.objs.pop(k)
        def list(self, prefix): return [k for k in self.objs if k.startswith(prefix)]
    s3 = FakeS3()
    m = b.backup(s3, src, "lif-sqlite", retain_days=14)
    assert "decisions.db" in m["files"] and "lif-sqlite/2000-01-01/old.db.gz" not in s3.objs   # pruned
    out = b.verify_latest(s3, "lif-sqlite")
    assert out["files"]["decisions.db"]["integrity"] == "ok"
    import gzip
    restored = tmp_path / "r.db"
    restored.write_bytes(gzip.decompress(s3.objs[m["files"]["decisions.db"]["key"]]))
    assert sqlite3.connect(restored).execute("SELECT count(*) FROM t").fetchone()[0] == 500
    con.close()
