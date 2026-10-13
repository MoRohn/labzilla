# Architecture

## Host reality that shapes everything

The host baseline is in `audit/GPU_BASELINE.md` *(private, local only)*.

- One GB10 GPU with **unified memory**. NVML reports memory as N/A, and CUDA allocations are not charged to cgroups.
- The primary workload's residents in Docker (Qwen2.5-32B bf16, FLUX image, embedding) pin about **107 GB**.
- Host MemAvailable is about 8–10 GiB.
- gpusched keeps 8 GiB of headroom, so **admissible GPU memory is about 0.6–1.2 GiB**.
- `gpusched` (a host systemd user unit) is the **single GPU admission authority**. LIF only reads it.
- CPU and GPU share one LPDDR5X bus. Production LLM decode (~3 tok/s on 67 GB of weights) is bandwidth-bound.

As a result, every LIF model runs on the **CPU** today: llama.cpp on the 10 Cortex-A725 cores (CPUs 0–4 and 10–14), with weights mmap'd.

## Planes

```
                     LAN (Traefik websecure)            in-cluster / host (cni0 10.42.0.1)
                              │                                     │
          ai.tiny-dgx.lan ────┤                 lif.tiny-dgx.lan ───┤
                              ▼                                     ▼
 DATA PLANE   ┌──────────── gateway ×2 ─────────────┐        CONTROL PLANE
              │ auth · rate limit · privacy · route │        ┌──────── controller ──────────┐
              │ primary yield · fallback · cache    │◄──────│ registry (SQLite, Longhorn)   │
              └───┬────────────┬───────────┬────────┘ /v1/  │ lifecycle · discovery (HF)    │
                  │            │           │       routing  │ availability probes · guard   │
                  ▼            ▼           ▼                │ Control Center UI             │
         tier0 (4B)   tier0-small (1.7B)  embedding         └──────┬────────────┬──────────┘
         ai-serving: CPU llama.cpp, model-store PVC               │ k8s API    │
                  ▲                                                ▼ (ai-serving only)
                  │ /v1/* (batch key)        decision-fabric ◄── batch engine
          batch engine (SQLite) ─────────────► Jev (api.typesafe.ai) / rules / local LLM
                                   │
                         gpusched /metrics (read-only token) → primary-workload state
```

| Service | Module | Storage | Depends on | If it fails |
|---|---|---|---|---|
| gateway | `lif.gateway.app` | none | model servers, gpusched (read) | Inference stops; 2 replicas with a PDB |
| controller | `lif.controller.app` | `lif-registry` (Longhorn) | k8s API, gateway, HF | Gateway keeps its last-known-good routing table; discovery, lifecycle and probes pause |
| decision-fabric | `lif.decision.app` | `lif-decisions` (Longhorn) | Jev, gateway (local LLM fallback) | Gateway, controller and batch run their own in-process fabric with rules |
| batch | `lif.batch.app` | `lif-batch` (Longhorn) | gateway, gpusched | `/v1/batch` returns 503; items resume from checkpoint |
| tier0 / tier0-small / embedding | llama.cpp `b11277` | `model-store` (local-path, read-only) | none | The router falls back along the alias chain, visibly |

All four Python services run from **one image** (`127.0.0.1:5000/lif/fabric:<tag>`). The Deployment chooses the app module.

## Request path (`/v1/chat/completions`)

1. Auth (gateway key) → rate limit → body size limit.
2. Route.
   - Alias → first healthy profile in the chain, or the canary at its configured percentage.
   - `local/auto` → the `request-route` decision (rules unless the caller declared PUBLIC).
3. Primary-workload yield.
   - While IMMINENT, CPU concurrency is 1 and `max_tokens` is capped at 512.
   - With a latency budget, `local/instant` is preferred.
4. Deterministic cache: non-streaming requests with explicit `temperature: 0`.
5. Proxy to llama.cpp.
   - On a 5xx or connect error, try the next profile in the chain. The fallback is recorded in `lif.reason`.
6. Accounting: tokens, tier (`fast_local` / `large_local` / `deterministic`), estimated avoided cost.

## Code map

| Path | Purpose |
|---|---|
| `lif/common/` | config loader (`/etc/lif` overrides the bundled `config/`), JSON logs, SQLite helper, metric catalogue |
| `lif/policy/engine.py` | data classes, content detectors, `may_send`, confidence gate, request budgets |
| `lif/decision/` | decision primitive, providers (Jev, rules, local LLM), fabric, DAG runtime, rules, service |
| `lif/gpu/state.py` | gpusched metrics → primary-workload state, `can_run()` |
| `lif/routing/router.py` | alias resolution, health, fallback, canary |
| `lif/models/` | HF client, hardware fit, registry, discovery, evaluator |
| `lif/controller/` | k8s client, manifest templates, lifecycle, API |
| `lif/batch/` | batch engine and API |
| `lif/cli/main.py` | `local-ai` CLI |
| `lif/knowledge/` | Persistent knowledge layer: compiler, graph, context, sessions, packages, MCP server, `knowledge` CLI ([KNOWLEDGE](KNOWLEDGE.md)) |
| `apps/control-center/index.html` | single-file UI with no external assets |
| `config/` | `lif.yaml`, `models.yaml`, `decisions/`, `workflows/` |
| `evals/core.yaml` | 15-item synthetic evaluation suite |
| `knowledge/` | Agent repos (`repos/`) and the local knowledge package registry (`registry/`); index in `.knowledge/` is derived |

## Namespaces and priorities

- `ai-system` holds the services and the registry. `ai-serving` holds the model servers and Jobs. `ai-batch` is reserved.
- The primary workload is **not** migrated.
- PriorityClasses: `workload-critical` (reserved for an optional external workload), `ai-critical`, `ai-interactive`, `ai-batch`, `ai-maintenance`, `ai-experimental`.
- These govern only CPU and RAM inside K3s. The GPU is gpusched's.

## Decision Engineering

The decision-fabric service also hosts the Decision Engineering runtime. It runs in-process, CPU only, and adds no pod. See [DECISION_ENGINEERING.md](DECISION_ENGINEERING.md).

| Piece | Where |
|---|---|
| Registry, releases, shadow, provenance, human queue | `decision-eng.db` on the decision-fabric PVC |
| Cascade: code → Jev → local reasoning → Kimi K3 → human/safe default | `lif/decision/cascade.py` |
| API `/de/*`, UI via the controller at `/v1/de/*` | `lif/decision/de_api.py`, Control Center → Decision Engineering |
| Improvement cycle every 6 h: mine → calibrate → recommend | `lif/decision/jobs.py` |

