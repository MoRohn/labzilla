# Troubleshooting

| Symptom | Likely cause | Check / fix |
|---|---|---|
| `401 missing or invalid API key` | wrong key, or Secret not mounted | `kubectl -n ai-system get secret lif-secrets`; keys are `name:key` lines in `LIF_GATEWAY_KEYS` |
| `503 no local model is deployed for local/vision` | by design (no capacity) | use another alias |
| `503 all models for <alias> are unavailable` | model pods down | `kubectl -n ai-serving get pods`, logs; `local-ai doctor` |
| Responses have `fallback: true` / `degraded: true` | primary unhealthy, memory guard shed it, or the alias floor is above the largest resident model | `local-ai status`; activity `memory_guard_shed` |
| `429 queue timeout waiting for a model slot` | concurrency limit during primary workload IMMINENT (1 per profile) or a backlog | `local-ai gpu`; retry later or send batch |
| `max_tokens` clamped to 512 | Primary-workload IMMINENT yield | by design while production runs |
| Everything slow, state stuck IMMINENT with reason "gpusched unreachable" | `primary_workload.enabled` is true and gpusched is down, or the token Secret is wrong. With the integration off this does not happen | Check the scheduler unit and `GET /metrics` with the metrics token. Or set `primary_workload.enabled: false` |
| gpusched shows `admissible 0` / `available` < 8192 MiB | host memory pressure | Rank processes by anonymous memory: `for p in /proc/[0-9]*; do awk '/^Pss_Anon/{print $2}' $p/smaps_rollup 2>/dev/null \| xargs -I{} echo {} $(basename $p); done \| sort -rn \| head`. On 2026-10-01 the cause was Firefox + desktop sessions |
| A new CPU model eats GB of anonymous memory | missing `--no-repack` | every llama.cpp server here needs `--no-repack` (GPU_SCHEDULING.md) |
| tier0-small keeps coming back then disappearing | `kubectl apply` resets replicas; the guard re-sheds after 60 s | expected; for Argo use `ignoreDifferences` on replicas |
| gpusched `unmanaged:<pid>` hold appears | something created a CUDA context without a lease | LIF pods must never use `runtimeClassName: nvidia`; find the pid's cgroup |
| Discovery shortlists nothing | Jev uncertain (P(advance) < 0.7 without rule agreement), or filters | Discovery page funnel → rejected reasons; candidates in "review" stay visible |
| Discovery `failed` | HF rate-limit or internet | the funnel shows the error; retry later |
| Benchmark refused "not enough host headroom" | MemAvailable − candidate anon < 9216 MiB | wait for headroom, or unload an optional model |
| Benchmark aborted, model back to STAGED | The primary workload became IMMINENT | by design; retry later |
| Control Center blank / 401 loop | wrong admin key | "Sign out", paste `secrets/lif-admin.key` |
| Pods can't pull `127.0.0.1:5000/...` | registry pod down | `kubectl -n ai-system get pods -l app=registry`; re-push the image |
| Config change not picked up | ConfigMap hash changed but pods weren't re-applied | re-run the kustomize apply |
