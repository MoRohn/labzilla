# Observability

LIF reuses the existing Argo-managed **kube-prometheus-stack** in `monitoring`. Its Prometheus selects ServiceMonitors and PrometheusRules in all namespaces. No second stack was added.

| Not deployed | Why |
|---|---|
| Loki | Every GiB comes from the same pool as the primary workload's models, and Grafana was already OOM-killed on 2026-09-30. Services log JSON to stdout (`kubectl logs`) |
| DCGM exporter | GB10 reports memory as N/A. gpusched's metrics (already scraped by `monitoring/extras/gpusched-scrape.yaml`) are the memory source of truth |

## Scrape targets (`deploy/k8s/base/14-monitoring.yaml`)

| ServiceMonitor | Targets |
|---|---|
| `ai-system/lif-services` | gateway, controller, decision-fabric, batch: `/metrics` every 30 s |
| `ai-serving/lif-model-servers` | llama.cpp `--metrics` (`llamacpp:*`) for each model server |

## Metric catalogue (`lif/common/metrics.py`)

| Metric | Labels | Meaning |
|---|---|---|
| `lif_tasks_total` | workload, tier | AI-eligible tasks by resolution tier: deterministic, jev, fast_local, large_local (≥ 14B), external, rejected |
| `lif_requests_total` / `lif_request_seconds` | endpoint, alias, status | gateway volume and latency |
| `lif_ttft_seconds` | alias, profile | streaming time to first token |
| `lif_tokens_total` | profile, direction | input/output tokens |
| `lif_decode_tokens_per_second` | profile | per-request decode rate (llama.cpp timings) |
| `lif_fallbacks_total` | alias, served_by, reason | non-primary serving |
| `lif_inflight_requests` | profile | |
| `lif_throttled_total` | reason (`queued`, `max_tokens_clamped`) | primary-workload yield actions |
| `lif_available` / `lif_probe_seconds` | capability | **inference-verified** availability (see below) |
| `lif_blerbz_state` | — | 0 LOW, 1 MODERATE, 2 HIGH, 3 IMMINENT. Series name kept so existing dashboards keep working. It is the optional primary-workload gauge; it stays at 0 while that integration is off |
| `lif_gpu_admissible_mib` | — | gpusched admissible |
| `lif_decisions_total` | decision, provider, action | |
| `lif_decision_seconds`, `lif_decision_confidence` | decision, provider | |
| `lif_jev_requests_total` | status | HTTP calls to Jev |
| `lif_jev_input_tokens_total`, `lif_jev_cost_usd_total` | — | ESTIMATED spend |
| `lif_decision_cache_total` | result | hit/miss |
| `lif_decision_escalations_total` | decision, to | |
| `lif_jev_circuit_open` | — | |
| `lif_privacy_blocks_total` | data_class, destination | |
| `lif_model_state`, `lif_alias_target` | model/state, alias/profile | |
| `lif_batch_items_total` / `lif_batch_queue_depth` | outcome / priority | |
| `lif_external_cost_avoided_usd_total` | tier | ESTIMATE (COST_MODEL.md) |

Heavy Model Avoidance:

```
sum(rate(lif_tasks_total{tier!~"large_local|external|rejected"}[1d])) / sum(rate(lif_tasks_total{tier!="rejected"}[1d]))
```

Today every local model is under 14B, so this is trivially 100 %. It becomes meaningful once a heavy tier exists.

## Availability (measured, not claimed)

Every 30 s the controller sends **real requests through the gateway**:

| Capability | Probe |
|---|---|
| `gateway` | `GET /v1/health` |
| `fast_model` | `local/fast` chat, 2 tokens |
| `default_model` | `local/default` chat. **A fallback answer counts as unavailable** |
| `embedding` | `local/embedding` |
| `decision_fabric` | `POST /decision/evaluate` (rules-backed, so it works without Jev) |
| `useful_local_ai` | fast OR default succeeded |

- Samples are stored in `registry.db` (`availability` table, 45 days). They are exposed at `GET /v1/availability?window_sec=` and in the Control Center Overview.
- Probes use `temperature 0.01` to bypass the deterministic cache, and `X-LIF-Workload: availability-probe` (filter that tag out of usage dashboards).
- SLOs (gateway ≥ 99 %, useful AI ≥ 95 %) are **not claimed**. The first samples (2026-10-01, n = 12) included a deploy window. Report only from ≥ 7 days of samples.

## Alerts (`PrometheusRule ai-system/lif-alerts`)

| Alert | Expression | For | Severity |
|---|---|---|---|
| LIFUsefulAIUnavailable | `max(lif_available{capability="useful_local_ai"}) == 0` | 5m | critical |
| LIFGatewayDown | `max(lif_available{capability="gateway"}) == 0` | 3m | critical |
| LIFDefaultDegraded | default down while fast up | 30m | warning |
| LIFJevCircuitOpen | `max(lif_jev_circuit_open) == 1` | 10m | warning |
| LIFHostHeadroomLow | `max(gpusched_mem_available_mib) < 8192` | 10m | warning |

Alertmanager routing is whatever the monitoring stack already has. No LIF-specific receivers are configured. Runbook: OPERATIONS.md.

## Logs and audit

- **Service logs:** JSON lines on stdout.
- **Autonomous and operator actions:** the registry `activity` table (`GET /v1/activity`, UI Registry page, `local-ai activity`).
- **Decisions:** `decisions.db` (30 days; `/decision/recent`, UI Decision Inspector).

**Pending:** Grafana dashboard panels for LIF metrics have not been built.

## Decision Engineering

Metrics, KPIs (decision offload rate, frontier generation avoidance rate) and four alerts are listed in [DECISION_ENGINEERING.md § Observability](DECISION_ENGINEERING.md#observability). The recording rules and alerts are in the `lif-decision-engineering` group of `deploy/k8s/base/14-monitoring.yaml`.

