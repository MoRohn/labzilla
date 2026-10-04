"""Hardware-fit engine: will this model run here, safely, and on which tier?

Estimates are built from the budgets MEASURED on this DGX Spark (private/lif/docs/audit/GPU_BASELINE.md, local only),
not from "128 GB". Fitting in the pool is not the same as being safe:

  total = weights + KV cache (context × concurrency) + runtime overhead
          (+ image projector + image-encoder headroom for vision models)
  CPU tier    anonymous memory is what counts (weights stay mmap'd with --no-repack), and
              the weights must be small enough to decode at a useful speed on 10 A725 cores
  GPU now     total + safety reserve ≤ gpusched admissible (≈1 GiB today)
  GPU window  total + safety reserve ≤ admissible + GRAMZ unload (~34.6 GB measured), and
              only as a preemptible gpusched command job
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from lif.common import config

GRAMZ_UNLOAD_MIB = 34_600          # measured 2026-09-30 (gpusched live unload)
CPU_ANON_BUDGET_MIB = 2_048        # per CPU model: KV + buffers + prompt cache (anon) — tier0 4B: 1.38 GiB + 0.25 cache
CPU_MAX_WEIGHTS_MIB = 6_144        # weights READ PER TOKEN beyond this: CPU decode on A725 cores drops below ~8 tok/s
# Mixture-of-experts models read only their active experts per token, so decode speed follows the active
# weights while memory follows all of them. Resident ceiling for one CPU model: the box keeps ~20 GiB free
# with every primary-workload resident loaded, and gpusched needs 8 GiB of it (2026-10-03, n=1).
CPU_MAX_RESIDENT_MIB = 12_288
# Vision (llama.cpp mtmd): the mmproj is read into anonymous buffers (not mmap'd), and encoding an
# image needs a compute buffer for the vision tower. ESTIMATE, not yet measured on this host:
# Qwen-VL class projectors at <=1024 px images. Replace with a measured number (date, n) after
# the first vision benchmark.
VISION_IMAGE_HEADROOM_MIB = 512
CPU_VISION_EXTRA_MAX_MIB = 2_048   # projector + image headroom allowed on top of the text anon budget
CPU_DECODE_GBPS = 50.0             # effective weight-streaming rate measured on the A725 set (2.4 GB × 21 tok/s)

# Runtime memory beyond KV, calibrated on tier0 (Qwen3-4B Q4_K_M, ctx 8192, 2 slots, 2026-10-01, n=1):
# 1,409 MiB anonymous = 576 KV + ~833 other. The other part is llama-server itself (process, tokenizer,
# per-thread arenas) plus the compute buffer, whose largest tensor is the logits of one ubatch
# (vocab × ubatch × fp32): 297 MiB for Qwen3's 151,936-token vocabulary.
RUNTIME_BASE_MIB = 512
UBATCH = 512
DEFAULT_VOCAB = 152_064            # unknown vocabulary → size as for a large one (Qwen/Llama-3 class)
# The pod's cgroup is charged for the mmap'd weights it touches (page cache) as well as its anonymous
# memory. A limit below weights + anon makes the kernel evict weights the next token needs: tier0 at
# 3,584 Mi re-read weights from disk on every token (2026-10-01; private/lif/perf, local only). So every
# limit gets this headroom on top.
CGROUP_HEADROOM = 1.15
# llama-server keeps idle slots' KV in a host-RAM prompt cache (--cache-ram) that DEFAULTS TO 8 GiB of
# anonymous memory. Every text server sets it explicitly to this, and it is part of the anon budget.
# Embedding servers set 0 (no prompts worth caching).
PROMPT_CACHE_MIB = 256
LIMIT_STEP_MIB = 256

BYTES_PER_PARAM = {"F32": 4.0, "F16": 2.0, "BF16": 2.0, "Q8_0": 1.07, "Q6_K": 0.82, "Q5_K_M": 0.71,
                   "Q4_K_M": 0.60, "Q4_K_S": 0.57, "MXFP4": 0.53, "FP8": 1.0, "INT4": 0.55}


@dataclass
class FitReport:
    verdict: str                  # fits_cpu | fits_gpu_now | fits_when_gramz_unloaded | no_fit
    weights_mib: int
    kv_mib: int
    overhead_mib: int
    total_mib: int
    anon_mib: int
    est_cpu_decode_tps: float | None
    reasons: list[str]
    mmproj_mib: int = 0           # image projector (anonymous memory), vision only
    image_headroom_mib: int = 0   # vision-encoder compute buffers (estimate)

    def to_dict(self) -> dict:
        return asdict(self)


def kv_cache_mib(meta: dict, context: int, concurrency: int, kv_bytes: float = 1.0) -> int:
    """KV = 2 × layers × kv_heads × head_dim × tokens × bytes. kv_bytes 1.0 = q8_0 cache."""
    L, H, D = meta.get("num_layers"), meta.get("num_kv_heads"), meta.get("head_dim")
    tokens = context  # llama.cpp splits --ctx-size across slots; total tokens = context
    if L and H and D:
        return int(2 * L * H * D * tokens * kv_bytes / 2**20)
    # unknown architecture: conservative ~0.1 MiB/token/B-params heuristic
    return int((meta.get("params_b") or 8) * 0.1 * tokens * kv_bytes / 8)


def runtime_overhead_mib(meta: dict, *, embedding: bool = False) -> int:
    """llama-server memory beyond weights and KV. Embedding servers pool hidden states, so they never
    allocate a vocabulary-sized logits buffer."""
    if embedding:
        return RUNTIME_BASE_MIB
    vocab = int(meta.get("vocab_size") or DEFAULT_VOCAB)
    return RUNTIME_BASE_MIB + int(vocab * UBATCH * 4 / 2**20)


def memory_limit_mib(weights_mib: float, anon_mib: float) -> int:
    """The container memory limit (= request) for a CPU model server: weights + anonymous memory +
    CGROUP_HEADROOM, rounded up to LIMIT_STEP_MIB."""
    need = (weights_mib + anon_mib) * CGROUP_HEADROOM
    return int(-(-need // LIMIT_STEP_MIB) * LIMIT_STEP_MIB)


def estimate(meta: dict, *, device: str = "auto", context: int = 8192, concurrency: int = 4,
             admissible_mib: float | None = None) -> FitReport:
    reasons: list[str] = []
    safety = int(float(config.get("gpu.safety_reserve_gb", 16)) * 1024)
    pb = meta.get("params_b")
    pick = meta.get("gguf_pick") or {}
    if pick.get("size"):
        weights = int(pick["size"] / 2**20)
    elif pb:
        weights = int(pb * 1e9 * BYTES_PER_PARAM.get((meta.get("precision") or "BF16").upper(), 2.0) / 2**20)
    else:
        return FitReport("no_fit", 0, 0, 0, 0, 0, None, ["parameter count unknown — cannot size safely"])
    # MoE: the per-token read is the active share of the weights (meta.active_params_b, from the model card)
    active = float(meta.get("active_params_b") or 0)
    read = int(weights * active / pb) if active and pb and active < pb else weights
    kv = kv_cache_mib(meta, context, concurrency)
    embedding = meta.get("category") in ("embedding", "reranking")
    overhead = runtime_overhead_mib(meta, embedding=embedding) + (0 if embedding else PROMPT_CACHE_MIB)
    mm = pick.get("mmproj") or {}
    mmproj = int(mm["size"] / 2**20) if mm.get("size") else 0
    img = VISION_IMAGE_HEADROOM_MIB if mmproj else 0
    vision = mmproj + img
    total = weights + kv + overhead + vision
    anon = kv + overhead + vision         # the projector is anonymous memory: it drives the headroom gates
    text_anon = kv + overhead
    tps = round(CPU_DECODE_GBPS * 1024 / max(read, 1), 1)

    def rep(verdict, tps_, why):
        return FitReport(verdict, weights, kv, overhead, total, anon, tps_, why, mmproj, img)

    cpu_ok = bool(pick) and read <= CPU_MAX_WEIGHTS_MIB and weights <= CPU_MAX_RESIDENT_MIB \
        and text_anon <= CPU_ANON_BUDGET_MIB and vision <= CPU_VISION_EXTRA_MAX_MIB
    if device in ("auto", "cpu"):
        if not pick:
            reasons.append("no GGUF artifact → not servable on the CPU tier (llama.cpp)")
        elif read > CPU_MAX_WEIGHTS_MIB:
            reasons.append(f"weights read per token {read} MiB > CPU tier max {CPU_MAX_WEIGHTS_MIB} MiB (≈{tps} tok/s)")
        elif weights > CPU_MAX_RESIDENT_MIB:
            reasons.append(f"resident weights {weights} MiB > CPU tier max {CPU_MAX_RESIDENT_MIB} MiB")
        elif text_anon > CPU_ANON_BUDGET_MIB:
            reasons.append(f"KV+buffers {text_anon} MiB > CPU anon budget {CPU_ANON_BUDGET_MIB} MiB; reduce context")
        elif vision > CPU_VISION_EXTRA_MAX_MIB:
            reasons.append(f"image projector + headroom {vision} MiB > CPU vision budget {CPU_VISION_EXTRA_MAX_MIB} MiB")
        if cpu_ok:
            extra = f" incl. {mmproj} MiB image projector + {img} MiB image headroom" if mmproj else ""
            return rep("fits_cpu", tps, reasons + [f"CPU tier: ~{tps} tok/s est., {anon} MiB anonymous{extra}"])
        if device == "cpu":
            return rep("no_fit", tps, reasons)

    adm = float(admissible_mib or 0)
    if total + safety <= adm:
        return rep("fits_gpu_now", None,
                   reasons + [f"GPU: {total} MiB + {safety} MiB reserve ≤ admissible {adm:.0f} MiB"])
    if total + safety <= adm + GRAMZ_UNLOAD_MIB:
        return rep("fits_when_gramz_unloaded", None,
                   reasons + [f"GPU only while GRAMZ is unloaded: {total} MiB + {safety} MiB reserve ≤ "
                              f"{adm:.0f} + {GRAMZ_UNLOAD_MIB} MiB (preemptible gpusched job)"])
    reasons.append(f"GPU: {total} MiB + {safety} MiB reserve > {adm:.0f} + {GRAMZ_UNLOAD_MIB} MiB even with "
                   "GRAMZ unloaded")
    return rep("no_fit", None, reasons)
