# Local Intelligence Fabric (LIF)

> Part of [labzilla](../README.md). Runs on the [`homelab/`](../homelab/) k3s platform. Keys are in the repo-root
> [`secrets/`](../secrets/README.md); sensitive docs are in the git-ignored `../private/lif/`.

The Local Intelligence Fabric is a small private AI platform on `tiny-dgx`, a DGX Spark (GB10, arm64, 128 GB unified memory). It runs on K3s next to the host's primary workload, which runs in Docker and keeps GPU priority.

Applications see a single OpenAI-compatible endpoint and ask for **logical models** such as `local/default`, never for physical Hugging Face IDs.

```
DETERMINISTIC CODE → JEV DECISION FABRIC → LOCAL FAST MODEL → LOCAL REASONING MODEL → KIMI K3 (configured, disabled) → HUMAN
```

## What runs today (2026-10-01)

| Component | Where | Notes |
|---|---|---|
| Gateway (2 replicas) | `ai-system/gateway` | OpenAI-compatible, API keys, primary-workload yield, fallback metadata |
| Controller + Control Center | `ai-system/controller` | Registry, lifecycle, discovery, availability probes, memory guard, UI |
| Decision Fabric | `ai-system/decision-fabric` | Hosted TypeSafe Jev (`jev-1.13.0`) plus rule fallbacks |
| Batch engine | `ai-system/batch` | Durable SQLite queue that yields to the primary workload |
| Tier 0 `qwen3-4b-instruct-2507-q4km-cpu` | `ai-serving/tier0` | CPU llama.cpp on the A725 cores, ~21 tok/s |
| `qwen3-1.7b-q8-cpu` | `ai-serving/tier0-small` | Optional fallback; the memory guard sheds it when host headroom is low |
| `qwen3-embedding-0.6b-q8-cpu` | `ai-serving/embedding` | 1024-dim embeddings |
| Local image registry | `ai-system/registry` | Bound to `127.0.0.1:5000` only |
| Labzilla Console | `ai-system/console` | Web console and mobile gateway (PWA) at `https://labzilla.local`; own sign-in and device pairing |
| Knowledge service | `ai-system/knowledge` | Knowledge read API for the console and Control Center; event sink off |

**No LIF model uses the GPU yet.** The primary workload's resident models pin about 107 GB, and gpusched admits about 0.6–1.2 GiB. See [GPU_SCHEDULING.md](docs/GPU_SCHEDULING.md) for the path to GPU tiers.

## Quickstart for application developers

```bash
KEY=$(cat ~/labzilla/secrets/lif-admin.key)        # or another gateway key, such as lif-workload.key
curl -sk https://ai.tiny-dgx.lan/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "X-LIF-Data-Class: CONFIDENTIAL" -H "X-LIF-Workload: my-app" \
  -H "Content-Type: application/json" \
  -d '{"model":"local/default","messages":[{"role":"user","content":"Headline for: ..."}],"max_tokens":64}'
```

`ai.tiny-dgx.lan` must resolve to `192.168.68.72` (there is no LAN DNS today; add it to `/etc/hosts`). Inside the cluster, use `http://gateway.ai-system.svc:8080`.

| Alias | Today served by | Notes |
|---|---|---|
| `local/instant` | 1.7B → 4B | Falls back to 4B while the memory guard has shed the 1.7B |
| `local/fast`, `local/default`, `local/batch` | 4B → 1.7B | |
| `local/reasoning`, `local/code` | 4B | Marked `degraded: true`: below the alias's size floor (30B / 14B) |
| `local/embedding` | Qwen3-Embedding-0.6B | |
| `local/vision`, `local/rerank` | none | Returns 503 with the reason |
| `local/web` | none yet → `local/default` | Grounded answers to live questions ([docs/WEB_GROUNDING.md](docs/WEB_GROUNDING.md)) |
| `local/auto` | `needs-live-data`, then `request-route` | Looks up live questions; otherwise picks the smallest tier that fits |

Request headers, all optional:

- `X-LIF-Data-Class`: `PUBLIC`, `INTERNAL`, `CONFIDENTIAL` (the default) or `RESTRICTED`. Only PUBLIC data is ever sent to Jev.
- `X-LIF-Workload`: an accounting tag.
- `X-LIF-Priority`: 2..8.

Every response says what served it. Non-streaming responses carry a `lif` object:

```json
"lif": {"requested": "local/default", "alias": "local/default", "served_by": "qwen3-4b-instruct-2507-q4km-cpu",
        "model": "unsloth/Qwen3-4B-Instruct-2507-GGUF@a06e946bb6b6", "fallback": false, "degraded": false,
        "blerbz": "HIGH", "latency_ms": 350.6}
```

Streaming responses carry `X-LIF-Served-By`, `X-LIF-Fallback`, `X-LIF-Degraded`, `X-LIF-Requested` and `X-LIF-Reason` headers, and `lif` appears in the final usage chunk.

## Documentation

| Doc | Topic |
|---|---|
| [ARCHITECTURE](docs/ARCHITECTURE.md) | Services, planes, data flow |
| [DEPLOYMENT](docs/DEPLOYMENT.md) | Build, push, apply, secrets, Argo CD |
| [MODEL_LIFECYCLE](docs/MODEL_LIFECYCLE.md) | Discovery → candidate → benchmark → canary → production → rollback |
| [JEV_DECISION_FABRIC](docs/JEV_DECISION_FABRIC.md) | Jev contract, privacy gate, thresholds, DAGs |
| [GPU_SCHEDULING](docs/GPU_SCHEDULING.md) | gpusched, primary-workload states, CAN_RUN, memory guard |
| PRIMARY_WORKLOAD_INTEGRATION *(private, local only)* | How the primary workload uses LIF; what LIF never touches |
| [MODEL_ROUTING](docs/MODEL_ROUTING.md) | Aliases, fallback, canary, `local/auto` |
| [BATCH_PROCESSING](docs/BATCH_PROCESSING.md) | `/v1/batch` |
| SECURITY *(private, local only)* | Keys, network policy, privacy |
| [OBSERVABILITY](docs/OBSERVABILITY.md) | Metrics, alerts, availability |
| [KNOWLEDGE](docs/KNOWLEDGE.md) | Persistent knowledge layer: typed agent repos, decisions/evidence/assumptions, reconsideration, sessions, MCP, packages |
| [OPERATIONS](docs/OPERATIONS.md) | Runbook, `local-ai` CLI, Control Center |
| [CONSOLE](docs/CONSOLE.md) | Labzilla Console: design, routes, security model, API, deploy & access (DNS, mDNS, TLS) |
| [UNDERSTANDING](docs/UNDERSTANDING.md) | Adaptive Understanding Compiler: ExplanationSpec IR, representation router, contract-checked renderers, explain CLI/API/MCP |
| [DECISION_ENGINEERING](docs/DECISION_ENGINEERING.md) | Decision mining, versioned Jev questions, calibration, cascade (code → Jev → local → Kimi K3 → human), shadow, agent SDK |
| [DISASTER_RECOVERY](docs/DISASTER_RECOVERY.md) | Reboot, outages, restore |
| [TROUBLESHOOTING](docs/TROUBLESHOOTING.md) | Symptoms → causes → fixes |
| [COST_MODEL](docs/COST_MODEL.md) | Estimates and how they are computed |

| PRODUCTION_READINESS *(private, local only)* | Measured results, acceptance status, risks, recommendations |
| audit/ *(private, local only)* | Phase 0 host baseline and risk register |

Private docs live in `../private/lif/docs/` on the host (git-ignored; see the root README).

Reproducible benchmarks live in [`benchmarks/`](benchmarks). Production-captured observations stay in `../private/lif/benchmarks/` *(private, local only)*.
