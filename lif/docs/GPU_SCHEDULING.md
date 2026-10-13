# GPU scheduling and resource protection

The primary-workload integration is **off by default** (`primary_workload.enabled` in `config/lif.yaml`). While it is off, Labzilla does not poll an external GPU scheduler, does not treat a missing scheduler as an emergency, and does not shorten answers. Turn it on only when another process on the same machine should keep GPU priority. The console label is `primary_workload.name` (default "Primary workload").

The rest of this page is how that integration behaves on a host that runs `gpusched`. It is not required to run Labzilla.

## Authority

`gpusched`, a host systemd user unit on `127.0.0.1:8770` and `10.42.0.1:8770` running in enforce mode, is the **only** GPU admission authority on this host. LIF is a read-only client.

- LIF reads `GET /metrics` with the gpusched **`metrics`** token (Secret `lif-gpusched`). That token can do nothing else.
- LIF pods never set `runtimeClassName: nvidia` or `NVIDIA_VISIBLE_DEVICES`. Any CUDA context of 256 MiB or more without a lease would raise gpusched's `unmanaged` hold, blocking the primary workload's own admissions.
- A metrics token is optional. `scripts/create-secrets.sh` reads `LIF_GPUSCHED_TOKEN_FILE`, then `~/.config/gpusched.token`, then the legacy path `~/.config/bnn/gpusched.token`. If none of those exist, it skips the `lif-gpusched` Secret and Labzilla keeps running with the integration off. gpusched reads new token lines only at startup.

Kubernetes PriorityClasses order CPU and RAM inside K3s only. The kubelet can't see unified GPU memory.

## Primary-workload capacity state (`lif/gpu/state.py`)

The state is polled every 5 s and treated as stale after 30 s.

| State | Condition |
|---|---|
| IMMINENT | a `gpusched_leases{klass="production"}` is live, OR a resident isn't loaded (reloading), OR gpusched is unreachable or stale (**fail-safe**) |
| HIGH | `gpusched_forecast_p_arrival_next_hour` ≥ 0.50 |
| MODERATE | P ≥ 0.20, or no forecast |
| LOW | otherwise |

The forecast is gpusched's own: validated, recency-weighted, with a backtest Brier score of 0.069. It rises ahead of the Stream It windows (ET 01:45–07:00, 07:45–10:30, 12:45–14:30, 17:45–19:30).

## CAN_RUN (`can_run(job, snapshot)`, deterministic)

| Job | LOW/MODERATE | HIGH | IMMINENT |
|---|---|---|---|
| P0–P1 (primary workload) | RUN | RUN | RUN |
| P2–P3 CPU (interactive) | RUN | RUN | RUN_DEGRADED |
| P4 CPU (async) | RUN | RUN | QUEUE (or RUN_DEGRADED if it can't wait) |
| P5 CPU (batch) | RUN | RUN | PAUSE_BACKGROUND |
| P6–P8 CPU (eval/experiments) | RUN | QUEUE | PAUSE_BACKGROUND |
| any GPU job | QUEUE: must go through a gpusched lease; REJECT if it can't wait and memory > admissible | | |

The P0–P8 levels map to gpusched classes as P0–1 → production, P2–3 → interactive, and P4–8 → background.

## CPU tiers: why, and their cost

- Today gpusched admits about **0.6–1.2 GiB**, so no LLM fits on the GPU.
- The CPU tiers run llama.cpp with a q8_0 KV cache and a hard cgroup memory limit. Writing a single answer runs on the **A725 cores** (`--cpu-mask 7C1F`, CPUs 0–4 and 10–14). Since 2026-10-01 (owner decision), prompt processing and batched decode run on the **X925 cores** (`--cpu-mask-batch F83E0`, CPUs 5–9 and 15–19), and chat servers repack their weights; embedding keeps `--no-repack`.
- Measured (Qwen3-4B Q4_K_M, n=3, production idle): prompt processing 51.8 tok/s (A725, no repack) → 218.6 tok/s (X925, repack); writing 26.9 → 29.3 tok/s with the split (A725 decode, repack).
- They create no CUDA context, so a GPU takeover can't evict them.

### The `--no-repack` lesson (measured 2026-09-30)

- By default, llama.cpp repacks quantized weights into **anonymous** memory.
- The 4B model held 3.1 GB anonymous. Three models dropped MemAvailable from ~10 to 3.9 GiB, PSI reached 4.2, swap grew, and gpusched admissible went to 0.
- With `--no-repack` the weights stay file-backed (reclaimable). 4B anon dropped to **~670 MiB**, at a cost of only **−5 % decode** (22.3 → 21.1 tok/s).
- Measured anon today: 4B ~670–850 MiB, 1.7B ~530–680 MiB, and embedding ~296 MiB after cutting its context to 4096 tokens over 2 slots.
- Vision models (CPU tier, `--mmproj`) also hold the image projector in **anonymous** memory, because llama.cpp reads it into buffers and doesn't mmap it. Sizing adds the projector plus 512 MiB of image-encoder headroom to total and anon. The 512 MiB is an **estimate, not yet measured**. The text-side anon budget (1,536 MiB) is unchanged, and the projector plus headroom gets its own 2,048 MiB cap. Estimated by `hardware_fit`, not measured (Qwen3-VL-4B Q4_K_M + Q8_0 projector, 8192 ctx): ~1.7 GiB anon and ~4.0 GiB total.

### The cgroup-fit lesson (2026-10-01)

- The pod's cgroup is charged for the mmap'd weights it touches **and** for its anonymous memory.
- tier0's anon grew from the ~670–850 MiB above to **1,409 MiB** after hours of chat (KV pages are touched lazily). With weights at 2,381 MiB, the 3,584 Mi limit sat full. The kernel evicted weight pages that the next token needed, so tier0 decoded from disk *(private/lif/perf/2026-10-01-tier0-latency.md, local only)*.
- Fix: `limit = (weights + anon) × 1.15`, rounded up to 256 MiB (`hardware_fit.memory_limit_mib`). The controller refuses a smaller limit, and `tests/test_serving_memory.py` checks every manifest.
- After the fix, the same pod decoded 19.7 / 21.0 tok/s and processed prompts at 84 tok/s, with no cgroup limit hits or refaults (n = 2 requests).
- Memory locking (`--load-mode mmap+mlock`) was tried and dropped the same day: once chat servers repack their weights (anonymous memory), the mmap'd file is read only at load, and locking it would pin ~2.4 GB of page cache that is never read again. Repacked weights are not evictable as page cache; under extreme pressure they can only be swapped.

### Prompt cache and speculation (2026-10-01)

- llama-server's `--cache-ram` (the host-RAM prompt cache for idle slots) **defaults to 8 GiB of anonymous memory**. Text servers set 256 MiB, which is counted in `anon_mib`; embedding servers set 0.
- `--cache-reuse 256` keeps the cached prefix when the console's context window drops the oldest turn, so a long conversation doesn't re-process its whole history.
- n-gram speculation (`--spec-type ngram-simple`, no draft model) was measured on tier0's settings and is **off**. Prose was unchanged at 21 tok/s, because nothing was drafted. Rewriting ~300 tokens of code fell from 19.8 to 9.2 tok/s: 28 % of drafted tokens were accepted, and verifying 48-token drafts on A725 cores costs more than it saves (n = 2 per arm). Profiles can still opt in with `spec_type`.

### Bandwidth yield

GB10's CPU and GPU share one memory bus. With the CPU tiers saturated (8 concurrent generations), production LLM decode went from **2.98 / 3.09 tok/s idle to 2.74 tok/s, about −10 %**. That is n = 1 per arm, so it is indicative only (`benchmarks/2026-10-01-production-llm-interference.md`, private, local only).

While the primary workload is IMMINENT, the gateway therefore:
- drops CPU concurrency to `yield.cpu_concurrency_busy` (1; `yield.cpu_concurrency_blerbz` is the older key);
- caps `max_tokens` at 512.

Batch P5 and above pauses.

Also measured: 4-way concurrency on the 4B model *lowers* aggregate throughput (21 → 16.4 tok/s), so its concurrency is set to 2.

## Host memory guard (controller, every 5 s)

LIF hands memory back **before** gpusched's 8 GiB headroom is crossed, whatever the cause.

| Group | Deployments | Shed below | Restore above |
|---|---|---|---|
| optional | `tier0-small` | 9216 MiB MemAvailable | 13056 MiB |
| secondary | `embedding` | 6144 MiB | 8704 MiB |
| hot fallback | `tier0` | never shed by the guard | — |

- Each condition must hold for 60 s, which avoids flapping.
- Shed state is persisted, so it survives a controller restart.
- There is no action without a trustworthy gpusched reading.
- **Observed 2026-10-01 07:21:** Firefox and interactive desktop sessions (~2 GiB) pushed MemAvailable to ~7.9 GiB. The guard shed `tier0-small`, and `local/instant` was served by the 4B with `fallback: true`.
- Even with that done, MemAvailable stayed below 8 GiB because of non-LIF processes. LIF alone cannot restore the headroom.

## Why there are no GPU tiers yet, and the concrete path

1. **GRAMZ unload only happens for `kind: command` jobs.**
   - In gpusched, `_maybe_unload_for` requires `spec.kind == "command"` and `preemptible`.
   - K8s-gated pods (the gate and hold sidecars) can **never** trigger the ~34.6 GB unload.
   - A GPU tier therefore needs a gpusched **command-job adapter**: a preemptible command (e.g. `docker run` or a scheduler-owned unit) that serves a llama.cpp-cuda or vLLM model, attributed by container, and is stopped on preemption.
   - That keeps gpusched as the authority. Unloads happen only at P(production) ≤ 10 %, the reload takes ~165 s, and the window shuts at reserved windows.
   - The hardware-fit verdict `fits_when_gramz_unloaded` marks models suitable for this path.
2. **Proposal (owner decision): requantize the production LLM.**
   - Serving Qwen2.5-32B as FP8 instead of bf16 frees about 30 GB.
   - It would likely also improve the ~3 tok/s decode behind Stream It's 39 % pre-fix failure rate.
   - It must keep the `:8100` contract (including the `adapter` field) and pass a primary-workload quality evaluation before cutover.
3. Any vLLM start needs an explicit memory size and a lease `mem_mb` that covers the load peak. Never use the default `gpu_memory_utilization`.

**For chat speed:** the shared production LLM (32B bf16, ~3 tok/s) behind `gpu-engine-bridge` would be slower than the CPU tier (~21 tok/s for the 4B once its memory limit fits), so it is a quality path, not a latency path. A faster-than-CPU GPU tier needs path 1 or 2 above.

**Status: not implemented or validated.** A live primary-workload GPU takeover of a LIF GPU model has not been tested, because no LIF GPU model exists.
