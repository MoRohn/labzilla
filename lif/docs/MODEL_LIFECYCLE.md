# Model lifecycle

The registry (`lif/models/registry.py`, SQLite on `lif-registry`) owns model state. Every change is an activity event with an actor and a reason.

## States and allowed transitions

| From | To |
|---|---|
| DISCOVERED | CANDIDATE, REJECTED, QUARANTINED |
| CANDIDATE | DOWNLOADING, REJECTED, QUARANTINED, DISCOVERED |
| DOWNLOADING | STAGED, FAILED, QUARANTINED |
| STAGED | VALIDATING, BENCHMARKING, REJECTED, DEPRECATED |
| VALIDATING | BENCHMARKING, FAILED, QUARANTINED |
| BENCHMARKING | APPROVED, REJECTED, FAILED, STAGED |
| APPROVED | CANARY, PRODUCTION, STANDBY, DEPRECATED |
| CANARY | PRODUCTION, APPROVED, REJECTED, STANDBY |
| PRODUCTION | STANDBY, DEPRECATED |
| STANDBY | PRODUCTION, CANARY, DEPRECATED |
| DEPRECATED | STANDBY, STAGED |
| QUARANTINED / REJECTED / FAILED | DISCOVERED (re-evaluate), REJECTED |

The registry itself enforces these invariants, not its callers:

- The revision must be a **40-char commit SHA**. `main` is rejected.
- A live (PRODUCTION/CANARY) row can't change revision. A new revision means a new profile.
- Only PRODUCTION, CANARY, APPROVED and STANDBY models may appear in an alias.
- A model referenced by an alias can't leave PRODUCTION, and can't be deleted.
- Rollback-critical models (the last `models.rollback_versions` + 1 alias versions) and pinned models are never deleted.
- Blocked models can't become CANDIDATE, DOWNLOADING, CANARY or PRODUCTION.

## Pipeline

```
Check for Better Models (UI button · local-ai models refresh · POST /v1/models/refresh)
  1  HF search per category (official REST API, unauthenticated)
  2  listing filter (no network cost): license tag, downloads ≥ 1000, toy/merge/adapter names,
     gated, trust_remote_code, pipeline_tag, category name regex, size range if known
  3  detail fetch (blobs=true) for ≤ 40 most-downloaded survivors per category
  4  full filter: params known, size in range, usable GGUF (Q4_K_M/Q5_K_M/Q8_0/Q4_K_S/Q6_K) with sha256
  5  hardware fit (measured budgets) → fits_cpu | fits_gpu_now | fits_when_gramz_unloaded | no_fit
  6  Jev screening DAG for every survivor, concurrently (2 Jev calls per model)
  7  policy recommendation + category-weighted priority → shortlist ≤ 5 → CANDIDATE
--- operator (or models.automatic_download) ---
  8  download Job: resumable, sha256-verified, atomic → STAGED (revision pinned)
  9  benchmark: temporary CPU server cand-<hash> (ai-maintenance) → VALIDATING → BENCHMARKING
 10  compare vs incumbent (policy thresholds) + Jev candidate-vs-incumbent (advisory) → APPROVED | REJECTED
 11  canary: N % of an alias → CANARY
 12  promote → PRODUCTION (old primary stays as the alias's fallback) · rollback any time
```

### Operator nomination

`local-ai models nominate <repo> --file <gguf> --category <cat>` (`POST /v1/models/nominate`) registers one specific GGUF as a CANDIDATE. It applies discovery's license, revision, toy-name and size gates plus hardware fit. The model then follows the normal download → benchmark → promote path. Category `web` (alias `local/web`) is nomination-only and is benchmarked on `evals/web.yaml` with its own policy. See [WEB_GROUNDING.md](WEB_GROUNDING.md#promoting-a-model-into-localweb). Mixture-of-experts models pass `--active-params-b`: decode speed is estimated from the active weights, and memory from all of them.

### Discovery categories (`lif/models/discovery.py`)

| Category | Search | Params (B) | Alias |
|---|---|---|---|
| fast | text-generation, gguf | 0.5–4.5 | local/fast |
| general | text-generation, gguf | 3–15 | local/default |
| coding | "coder", gguf (name must contain "cod") | 1–15 | local/code |
| reasoning | "thinking" / "reason", gguf | 3–35 | local/reasoning |
| embedding | feature-extraction / "embedding", gguf | 0.05–8 | local/embedding |
| reranking | "reranker", gguf | 0.05–8 | local/rerank |
| vision | image-text-to-text + "GGUF"; "VL", gguf; "vision", gguf. Name must say VL/vision/omni, or the repo is tagged image-text-to-text | 1–9 | local/vision |

Vision has one extra gate: a sibling `*mmproj*.gguf` image projector with a size and an LFS sha256. The picker takes Q8_0, then F16, then BF16. F32 and projectors with no precision in the name are never picked. A repo without one is rejected as `no image projector`. The listing stage already applies this when the search listing includes file names. The profile carries `mmproj: {file, sha256, size}`.

### Measured funnel (live run, 2026-09-30)

| Category | Listed | After listing filter | After deterministic + fit | Shortlisted |
|---|---|---|---|---|
| fast | 200 | 63 | 2 | 0 |
| embedding | 368 | 98 | 6 | 4 |
| coding | 200 | 85 | 7 | 5 |
| general | 200 | 82 | 3 | 0 |

The four categories took about 4.4 s in total. Jev screening took about 0.2–0.7 s per category. Jev decides only on the few survivors of the code filters, never on all 968 listings. Nothing was downloaded.

## Gates (code, not AI)

| Gate | Rule |
|---|---|
| Download | State CANDIDATE. Repo, file and revision pass `templates.validate_profile`, and so does the mmproj file name for vision. A sha256 is required for every file. The Job fetches the weights and then the projector: each file resumable, verified and atomically renamed. Any mismatch quarantines the model. The operator, or the `automatic_download` setting, must ask |
| Benchmark (candidate) | Primary workload LOW/MODERATE. Not in maintenance. `MemAvailable − candidate anon ≥ 9216 MiB` (gpusched's 8 GiB + 1 GiB). Aborts when the primary workload becomes IMMINENT (state goes back to STAGED) |
| Benchmark (live model) | Runs against its existing endpoint; no temporary server |
| GPU-tier benchmark/load | Refused. It needs a gpusched command-job window (GPU_SCHEDULING.md) |
| Approve | `evaluator.compare` against the incumbent: quality regression ≤ 0.01, TTFT regression ≤ 10 %, memory increase ≤ 20 %, structured-output ≥ min(0.98, incumbent's), errors ≤ 0. CANARY only if it is also better (quality up, or equal quality with decode +10 %); HOLD if merely safe; otherwise REJECT. With no incumbent (an empty alias, e.g. the first vision model): errors ≤ 0, structured-output ≥ 0.9 × 0.98 when the suite has structured items, and quality ≥ `min_quality_no_incumbent` (0.5) |
| Promote | State CANARY/APPROVED/STANDBY **and** a local benchmark exists. Automatic promotion is off unless enabled |

Jev's `candidate-vs-incumbent` verdict is stored in the comparison report, but it never decides.

## Evaluation (`evals/core.yaml`, `lif/models/evaluator.py`)

- 15 synthetic items covering instruction following, news-style summaries and headlines, story classification and urgency, JSON extraction, arithmetic reasoning, code and a hallucination trap.
- All checkers are deterministic (contains / regex / choice / number / json).
- Latency is measured over streaming: TTFT, decode tok/s from llama.cpp timings, plus a 2–4-way load pass.
- Embedding models get a latency check and a paraphrase-versus-unrelated sanity check instead.
- Vision models get `evals/vision.yaml`: 6 items covering colour, a split image, two block-drawn digits and counting squares. The PNGs (256–384 px) are generated at run time with stdlib `zlib`/`struct` and sent as `image_url` data URIs. The suite reports `structured_ok: null` (it has no JSON items), plus the same latency summary. Its benchmarks are stored under the suite `vision`.

| Profile | Quality | TTFT p50 | Decode p50 | Aggregate @4 | Errors |
|---|---|---|---|---|---|
| qwen3-4b-instruct-2507-q4km-cpu | 0.80 | 55 ms | 21.3 tok/s | 16.4 tok/s | 0 |
| qwen3-1.7b-q8-cpu (thinking off) | 0.93 | 431 ms | 35.5 tok/s | 49.1 tok/s | 0 |

Fifteen items are not enough to repoint `local/fast`. A larger suite and a canary must decide.

## Retention (`Lifecycle.gc`, daily)

- **Removed:** artifacts of REJECTED, FAILED, DEPRECATED, QUARANTINED and idle STAGED models older than 7 days, by a GC Job.
- **Kept:** registry rows, which are the evidence.
- **Never removed:** pinned models, alias-referenced models, and rollback-critical models.

## Seeded state

The three Tier 0 profiles were seeded as PRODUCTION from `config/models.yaml`, and the aliases were seeded as version 1. Their benchmarks are in `benchmarks/`.

The controller's own benchmark records start empty. Run `local-ai models benchmark <id>` so promotions have an incumbent baseline.
