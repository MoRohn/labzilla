# Adaptive Understanding Compiler

Labzilla answers "help me understand this" by building **one canonical explanation IR** and then choosing
the cheapest representation that communicates it: 100 words, a table, a causal diagram, an interactive
simulation, or a small combination. Every representation is compiled from the same IR. A renderer that
adds a fact, drops an uncertainty or draws a relationship the IR lacks is rejected.

```text
question ─▶ builder ─▶ KnowledgeModel ─▶ planner ─▶ ExplanationSpec/v1 ─▶ validate
                                                         │
                     features (code) + 3 Jev judgments ──▶ router ─▶ renderers ─▶ contract ─▶ critic
                                                         │                         (per artifact) (across artifacts)
                                                   SQLite: specs │ artifacts │ sessions │ feedback
```

Code: `lif/lif/understanding/`. Packages: `lif/explanation-packages/`. Decisions:
`lif/decision-packages/understanding/`. Eval set: `lif/evals/understanding/`.

## Status

| Spec phase | State | Where |
|---|---|---|
| 1 ExplanationSpec + validation | Done | `spec.py`, `validate.py` |
| 2 Knowledge model → spec | Done: package, live GPU state and local-LLM builders, code planner | `knowledge_model.py`, `collect.py` |
| 3 Analysis + router | Done: utility scoring, marginal value, Jev hooks, deferral | `analysis.py`, `router.py`, `registry.py` |
| 4 STE + Mermaid | Done | `render/prose.py`, `render/mermaid.py` |
| 5 Excalidraw + static HTML | Done | `render/excalidraw.py`, `render/html.py` |
| 6 Interactive renderer / canvas | Partial: step-through page. Glimpse-style canvas and highlight-to-ask UI are not built; the API accepts `selected` element ids | `render/html.py` |
| 7 SimulationSpec + mini-app | Partial: deterministic simulations; generated mini-apps are off | `simulate.py`, `render/html.py` |
| 8 Manim / video | Not built. Declared in the registry as unavailable, so the router explains why it did not pick them | `registry.py` |
| 9 Critic + consistency | Done: contract (per artifact), critic (across artifacts). No vision critic | `render/base.py`, `critic.py` |
| Feedback → routing (§130 last step) | Partial: overrides, simplify/deepen and helpful/abandoned feedback are recorded, with override rate per primary renderer. Nothing adjusts the weights automatically yet | `store.py` |
| 10 Ask/UI integration | Partial: API, CLI and MCP. The console serves the API (enabled 2026-10-02); there is no UI yet | `api.py` |

| Acceptance test | State | Test |
|---|---|---|
| A: "What is Kubernetes?" gets prose, no video | Pass (an available fake video renderer is not picked) | `test_acceptance_a_*` |
| B: pods not reaching GPUs gets prose + causal diagram from cluster state | Pass. "These pods…", "right now" and "this cluster" questions go to the live `state:gpu` builder; the general phrasing gets the generic package. Checked on the host 2026-10-02 (builder `state:gpu`). Without `LIF_GPUSCHED_TOKEN_FILE` the host run cannot read gpusched, so it says less and with lower confidence | `test_acceptance_b_*`, `test_live_state_builder_dogfood` |
| C: reservation vs throughput gets a simulation | Pass | `test_acceptance_c_*` |
| D: "Teach me attention" gets progressive explanation + diagram + animation | Partial: summary + step diagram from the real local model (`local/default`, a 4B CPU model; checked 2026-10-02 with the primary workload live, about 45 s per run, n=3 runs while fixing it). No animation renderer. Accuracy is bounded by the local model | `test_llm_builder_*` |
| E: four formats from one spec are consistent | Pass, including detection of an emphasis contradiction and an invented edge | `test_acceptance_e_*` |
| F: simpler, without re-research | Pass (`builder_calls` stays 1) | `test_acceptance_f_and_g_*` |
| G: engineer → executive from the same semantics | Pass | `test_acceptance_g_*` |
| H: offline: prose, Mermaid, Excalidraw, HTML, simulation | Pass for package and live-state questions. Novel questions need the local model | `test_acceptance_h_*` |
| I: GPU renderers yield to the primary workload | Pass with a fake GPU renderer. No real GPU renderer exists yet | `test_acceptance_i_*` |
| J: a renderer that introduces a claim is rejected | Pass for new words, new numbers, added negations or quantifiers ("does not", "all"), unknown ids and dropped uncertainty. Reordering existing words is not caught (see the contract limits) | `test_acceptance_j_*` |

## ExplanationSpec/v1

| Group | Elements (each has a stable `id`, unique across the spec, and a `level` 0–3) |
|---|---|
| Question | `question`, `audience`, `learning_objectives`, `summary {headline, claims}`, `checkpoints` |
| Meaning | `concepts`, `claims` (observed / inferred / definition / assumption / general, with confidence and importance), `relationships` (typed), `causal_chains`, `processes`, `timelines`, `hierarchies`, `comparisons` |
| Quantities | `variables`, `equations`, `simulation` (a registered deterministic primitive bound to variables) |
| Teaching | `examples` (and counterexamples), `analogies`, `misconceptions`, `constraints` |
| Trust | `evidence` → `source_refs` (knowledge key `repo::slug`, metric, k8s, log, URL, state path), `uncertainties` |
| Wording | `terms` (canonical term, abbreviation, synonyms, plain gloss) |
| Presentation | `render_hints` (enumerations only), `metadata` (builder, data class, parent, snapshots) |

Depth levels: 0 glance (15 s), 1 summary (45 s), 2 learn (180 s), 3 deep (600 s). A renderer at depth *d* shows
elements with `level ≤ d`. `semantic_hash()` covers meaning only, not metadata or hints. `migrate()` is where
v1 → v2 will go.

The validator runs before anything is rendered:

| Check | Severity |
|---|---|
| Duplicate ids; references to missing ids or ids of the wrong kind | error |
| Claim with no concept; observed claim with no evidence | error |
| Claim below 0.9 confidence without an `uncertainty`; uncertainty confidence ≠ claim confidence | error |
| Causal chain under 2 steps; empty process; unordered timeline | error |
| Equation symbol not declared as a variable (the left-hand side is defined by the equation) | error |
| Simulation primitive, bindings, controls or outputs that do not match the primitive | error |
| Source ref that does not resolve (syntax offline, or `knowledge_resolver` against the knowledge index) | error |
| Term collisions | error |
| Inferred claim with neither evidence nor uncertainty; claim nothing uses; no learning objectives | warning |

## Router

Features come from code (`analysis.py`): counts, relationship density, and structure strengths in 0–1 for
definition, causal, process, temporal, hierarchy, comparison, dependency, spatial, quantitative, parameter
and narrative. Question type comes from rules (definition, causal, debug, procedural, comparative, temporal,
parameter, teach).

| Term | Meaning |
|---|---|
| `fit` | strongest structure among the renderer's `best_for` |
| `0.55 + 0.35·clarity` | how directly the renderer communicates what it fits |
| `interactivity`, `learning` | only for renderers that show parameter-driven outcomes; scaled by whether the reader has time to use the controls |
| `audience`, `viewport` | executive: less interaction and mechanism. Novice: prose and diagrams. Mobile: no wide sketches |
| `latency`, `overload`, `cost` | render time and reading time against the budget; resource class (LIGHT 0 … LONG_RUNNING 0.4) |
| `unsupported` | a strong structure the renderer cannot show |

The top renderer is primary. A short STE summary always comes first (fast first response, and the text
alternative). Other renderers are added only when `0.8·new structure + 0.2·utility − load − latency − cost ≥ 0.30`,
up to 2 / 3 / 4 artifacts for budgets ≤ 45 s / ≤ 240 s / longer. The plan's `why` lists what was chosen and why
the next-best and video were not. GPU and long-running renderers are **deferred** while gpusched reports the
primary workload as HIGH, IMMINENT or unknown.

| Jev decision | Sees | Effect |
|---|---|---|
| `understanding-prose-sufficient` | `features.question_kind`, `features.structure` | `yes` → non-prose × 0.75 |
| `understanding-interaction-worthwhile` | `features.has_simulation`, `.question_kind`, `.structure` | `no` → interactive × 0.6 |
| `understanding-diagram-family` | `features.question_kind`, `features.structure` | Mermaid/Excalidraw layout |

Jev sees only derived features (PUBLIC), never question text or cluster state. Non-actionable answers are
ignored. The three decisions are at `stage: designed`, and a bare decision name only resolves to a
`production` version, so until a person promotes them the rules in `lif/decision/rules.py` answer, online and
offline.

## Renderers

| Renderer | Target | Resource | Output | Verification |
|---|---|---|---|---|
| `ste-prose` | STE_PROSE | LIGHT | text | contract |
| `structured-prose` | STRUCTURED_PROSE | LIGHT | markdown: short version, observed / inferred, examples, misconception, uncertainty, sources | contract |
| `mermaid` | MERMAID | CPU | flowchart (causal, process, dependency) or timeline, with accTitle/accDescr; inferred edges dashed; uncertainty notes | contract, static syntax |
| `excalidraw` | EXCALIDRAW | CPU | scene JSON; the renderer owns layout and bindings | contract, no-overlap, bindings |
| `table` | TABLE | LIGHT | markdown comparison | contract |
| `static-explainer` | STATIC_VISUAL_EXPLAINER | CPU | self-contained HTML, no script | contract, CSP |
| `step-through` | INTERACTIVE_HTML | BROWSER | one step at a time, keyboard and buttons | contract, CSP |
| `simulation` | SIMULATION | BROWSER | controls bound to variables; outcomes precomputed by `simulate.py` | contract, CSP, grid |
| `manim`, `narrated-video`, `glimpse-canvas`, `mini-app` | — | GPU / LONG_RUNNING / BROWSER | declared, unavailable | — |

Render time on this host (2026-10-02, n=50 per renderer, synthetic packages at depth `learn`, in-process):

| Renderer | p50 ms | p95 ms | Size KiB |
|---|---|---|---|
| `ste-prose` | 0.65 | 0.66 | 0.7 |
| `structured-prose` | 1.15 | 1.16 | 1.2 |
| `mermaid` | 0.73 | 0.75 | 0.8 |
| `excalidraw` | 0.92 | 0.97 | 20.4 |
| `table` | 0.73 | 0.74 | 0.3 |
| `static-explainer` | 1.26 | 1.29 | 4.7 |
| `step-through` | 0.93 | 0.93 | 4.7 |
| `simulation` | 18.46 | 18.54 | 82.0 (1,248-point grid) |

Mermaid output was checked once with mermaid.min.js in headless Chromium (2026-10-02): 40 of 40 diagrams
(4 packages × 4 depths × forced families) parsed and rendered. That check is not part of CI.

### The renderer contract

`render/base.py:check_contract` runs on every result. It fails the artifact when a segment:

| Fault | Example |
|---|---|
| cites an id the spec lacks | `refs=["c-nope"]` |
| carries meaning but cites nothing | an untagged sentence |
| uses a content word or number its cited elements do not contain | "memory fragmentation", "9 minutes" |
| shows a claim without that claim's uncertainty | headline without "Confidence 78%" |

Structural words ("because", "observed", "confidence") come from a fixed vocabulary. It has no negations,
quantifiers or modals, so "not", "all", "only" and "must" have to come from the cited text. Terminology from `terms`
is always allowed. Limits: the check works on words and numbers, so it catches new facts. It does not catch
a rearrangement of existing words into a different claim. The critic's emphasis and edge checks cover the
most important case of that (§68): every artifact must lead with the headline and draw only spec edges.

A renderer that lacks something it needs returns `needs_semantic_update`. A crash or a contract failure
returns `failed`, and the compiler falls back (simulation/interactive → static explainer → structured prose).

## Builders

| Builder | When | Model |
|---|---|---|
| `state:gpu` | "GPU utilization … now / on the DGX", or `context.kind = "gpu"` | none. Reads gpusched, nvidia-smi, /proc/meminfo, `kubectl get pods`, all read-only |
| `package:<pkg>/<name>` | a pattern in `explanation-packages/` matches | none |
| `llm:local` | otherwise | the local gateway (`local/default`), CONFIDENTIAL, never external. Asks for compact, grammar-constrained JSON (`response_format: json_object`); `normalize()` fixes shape mistakes without adding facts (headline text → claim id, dangling references dropped, unknown enum values → neutral, an unexplained low confidence gets an uncertainty saying so); one repair round. While the primary-workload integration is on and that workload is live, the gateway caps completions at `yield.max_tokens_busy` (512; the older key `yield.max_tokens_blerbz` still works); a cut-off answer gets its own retry asking for less, then a plain error |

On the host, set `LIF_GPUSCHED_TOKEN_FILE` to a readable copy of gpusched's metrics token. `GpuStateWatcher`
prefers it over the configured `gpusched.token_file`, and it is unset in the cluster. The collector and the
router's resource probe share one gpusched read per 5 s, so both see the same primary-workload state. Only pods
the scheduler marked `PodScheduled=False` count as unschedulable; a pod pulling its image does not. The GPU builder never states what it could not read. With gpusched unreadable it drops lease and state
claims, lowers confidence and lists "gpusched metrics (read failed)" as evidence needed.

## Interfaces

| Surface | Use |
|---|---|
| CLI | `local-ai explain "Q" [--format F] [--profile quick\|standard\|deep\|teach] [--audience A] [--mobile] [--offline]`, `local-ai explain ID --simplify \| --deepen \| --interactive \| --video \| --show-ir \| --evaluate \| --history`, `local-ai explain --renderers \| --lint-packages`. `labzilla explain …` forwards on the host |
| HTTP | `uvicorn lif.understanding.api:app --host 127.0.0.1 --port 18084`: `POST /v1/explain` (`?stream=1` for SSE), `GET /v1/explanations/{id}[/history]`, `POST /v1/explanations/{id}/render\|simplify\|deepen\|evaluate`, `POST /v1/sessions/{sid}/feedback`, `GET /v1/sessions/{sid}/artifacts/{renderer}`, `GET /v1/renderers` |
| Console | the same routes under `/api/v1/…` behind session auth (`ask`), enabled in `deploy/k8s/base/16-console.yaml` with `LIF_CONSOLE_UNDERSTANDING=1` (remove it to turn the API off). There is no UI for it yet. In the pod there is no kubectl and no gpusched token, so live GPU explanations report those sources as unreadable. Inside the console the store defaults to `understanding.db` next to `LIF_CONSOLE_DB`, and local generation uses `LIF_CONSOLE_GATEWAY_KEY` |
| MCP | `lif-understanding` in `.mcp.json`: `explanation_create`, `_get`, `_render`, `_simplify`, `_deepen`, `_evaluate`, `_renderers` |

Stream events: `analysis.started`, `knowledge_model.ready`, `explanation_ir.ready`, `summary.ready`,
`route.ready`, `<target>.rendering`, `<target>.ready`, `<target>.failed`, `evaluation.ready`, `done`, `error`.

Storage: `$LIF_UNDERSTANDING_DB` (default: next to `$LIF_CONSOLE_DB` when set, else `~/.local/share/lif/understanding.db`) holds specs, artifacts
(keyed by semantic hash, renderer and version, theme, audience, depth, viewport, selection), sessions and
feedback. Specs built from live state are CONFIDENTIAL. Keep saved ones under
`private/lif/understanding/` *(private, local only)*.

## Security

| Concern | Control |
|---|---|
| Generated pages | CSP meta `default-src 'none'`, `connect-src 'none'`; no external URLs; static check for network APIs |
| Serving | `GET …/artifacts/…` adds `Content-Security-Policy: sandbox allow-scripts …; frame-ancestors 'self'` and `X-Frame-Options: SAMEORIGIN`: an opaque origin that only a same-origin page may frame. Never inline artifacts in the console origin |
| Secrets | renderers take only the spec; no credentials or environment reach a page |
| Data class | Jev gets derived features (PUBLIC); builders run local only |

## Kubernetes (proposed, not deployed)

Today every available renderer is an in-process template compiler: LIGHT or CPU, at most about 20 ms.
A separate worker earns its keep only when a renderer needs a browser, a GPU or minutes of runtime.

| Worker | Renderers | Resource class | Priority |
|---|---|---|---|
| `understanding-control` (in the console or its own pod) | prose, Mermaid, Excalidraw, table, HTML, simulation | LIGHT/CPU | interactive explanation |
| `renderer-browser` | rasterise Mermaid, screenshots for a vision critic | BROWSER | artifact generation |
| `renderer-video` | Manim, TTS, narrated video | GPU / LONG_RUNNING, leases through gpusched | video rendering, below every primary-workload class |

Each worker needs requests and limits (single node, 128 GB shared). GPU work leases through gpusched, and
deploying any of this is the owner's call.

## Extending

| Add | How |
|---|---|
| A renderer | a class with `capabilities()`, `accepts(spec)`, `render(RenderRequest)` emitting `Segment`s, registered in `registry.default_renderers()`. The router needs no change |
| A simulation primitive | `@simulate.primitive(name, inputs, outputs)` on a pure function |
| An explanation package | `explanation-packages/<pkg>/<name>.yaml`, then `local-ai explain --lint-packages`. Shipped packages are generic: no evidence, no observed claims (a test enforces this). Synthetic scenarios with invented observations go in `tests/fixtures/understanding/` |
| A router case | `evals/understanding/router_cases.yaml`, then `pytest tests/test_understanding_interfaces.py` |
