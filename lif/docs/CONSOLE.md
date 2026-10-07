# Labzilla Console

The Labzilla Console is the human front door to LIF. It is a minimal, cross-platform **local AI operating console**: on a desktop it gives full depth, on a phone it gives immediate access and control, and the command bar is the fastest path to everything. AI complexity sits behind logical capabilities, infrastructure complexity sits behind human-readable states, and technical detail appears only on demand (spec §111).

It is additive. The existing Control Center (`apps/control-center`, served by the controller) stays as it is.

| | |
|---|---|
| Backend (BFF) | `lif/console/` — FastAPI in the existing LIF image, 1 replica, SQLite on its own volume |
| Frontend (PWA) | `apps/console/` — Preact 10 + TypeScript + Vite, a static bundle served by the BFF |
| Contracts | `lif/console/contracts.py` → `apps/console/src/api/contracts.gen.ts` (`python -m lif.console.gen_ts [--check]`) |
| Tests | `tests/test_console.py`; e2e harness with fake upstreams in `apps/console/e2e/` |

The spec section numbers (§) below refer to the owner's UI/UX brief for the console.

## 1. Principles

| Rule | What it means in the console |
|---|---|
| Minimum information for the next decision (§4) | Home is the owner's cockpit (§6, changed 2026-10-01 at the owner's request): the status block, then eight KPI tiles with 6-hour trend lines, recent conversations, attention, services and folded activity. Every tile and row is a drill-down. No chart library: trends are inline SVG |
| Five-second comprehension (§110) | Every screen leads with a sentence in plain language ("Running on fallback model") before any detail |
| Know before you act (§98, §110) | Every action button states its consequence, as a subtitle or an `ActionPreview`. Dangerous ops use `ConfirmDialog`; routine safe actions never confirm (§41) |
| Detail on demand (§42, §79) | Raw states, pod names, revisions and request ids live only in `tech: TechDetail[]`, shown by the Technical Details drawer |
| Honest states | When data does not exist upstream, the console says so with a reason ("No GPU temperature sensor is exported yet"). It never invents a value, an agent or an action |
| Server is authoritative (§70, §89) | Threads, sessions and approvals live on the server. `localStorage` only holds conveniences (theme, collapsed nav) |
| Local first (§64, §75) | The browser talks only to the console. Prompts are never sent off-box unless the user marks a prompt "Allow Jev routing" |
| Advanced exists, quietly (§97) | Operator controls are in Models, Jobs and System → Settings, behind admin permissions, never on Home |

## 2. Information architecture

### Navigation (§5, §45)

| Desktop / tablet side nav | Phone bottom nav | Purpose |
|---|---|---|
| Home | Home | Is Labzilla healthy, what AI is ready, how fast and available it is, what the DGX is doing, what I asked, what happened |
| Ask | Ask | Prompt → route → stream, with the receipt |
| Agents | Agents | Agent work, approvals, runnable agents |
| Models | More → Models | Roles first (Fast, Balanced, Deep, Code…), then discovery and candidates |
| Jobs | More → Jobs | Batch and background work, grouped by state |
| Knowledge | More → Knowledge | Decisions, evidence, assumptions, recent changes |
| System | More → System | Compute, services, storage, network, logs, settings |
| Connect a phone (side-nav footer, admin) | More → Connect a phone, Trust this device | Pairing, devices, certificate and install status. Trust is the connection line in the side-nav footer |

### Where the technical areas live (§5: contextual, never top-level)

| Area | Where it appears |
|---|---|
| GPU / unified memory / BLERBZ | Home status block; System → Compute; command bar ("Why is the GPU busy?"); palette "Open GPU" |
| K3s / Kubernetes | System → Services (human health); raw pod/deployment state in Technical Details |
| Decision Fabric (Jev) | Ask receipt (`DecisionBadge`); Agents → decision cycle runs and review tickets |
| Hugging Face | Models → Check for better models (discovery) and candidate detail ("Source: Hugging Face") |
| Benchmarks | Model detail and candidate comparison |
| Logs | System → Logs (relevant events first); "View raw logs" note |
| Earn (earning system, namespace `earn`) | System → Services, Logs, Storage and Alerts, read-only. See [Earn on System](#earn-on-system) |

### Routes

| Route | Screen | Loading |
|---|---|---|
| `/` | Home (compact width → Mobile Gateway, §12) | eager |
| `/ask`, `/ask/:id` | Ask, one thread | eager |
| `/agents`, `/agents/:id` | Agent runs and approvals; run detail | lazy |
| `/models`, `/models/roles/:role`, `/models/deployments/:id` | Portfolio, role detail, physical model detail | lazy |
| `/models/discovery`, `/models/candidates/:id` | Check for better models; candidate comparison | lazy |
| `/jobs`, `/jobs/:id` | Jobs, job detail | lazy |
| `/knowledge`, `/knowledge/o/:key` | Knowledge home; decision record or object | lazy |
| `/system`, `/system/:tab` | Tabs: compute, services, storage, network, logs, settings | lazy |
| `/connect`, `/trust` | Connect Mobile (QR, URL, devices); Trust this device | lazy |
| `/setup`, `/login`, `/pair` | First run, sign in, phone side of pairing (no shell) | lazy |

### Earn on System

The earning system (`~/arbies`, namespace `earn`) runs on this node, so System watches it like a LIF workload.
The console reads earn's `GET /api/status` with `LIF_EARN_READ_KEY` (Secret `earn-console`) every 30 s. It holds
no Earn control key. Stop, resume, clearing a trip and acknowledging a rule change stay with earn's CLI (arbies RUNBOOK §3).

| System tab | What appears | Source |
|---|---|---|
| Services: **Earning system** | `paused` when an operator stopped it, with the reason and the open safety-stop count. `attention` when it isn't started or isn't safe to trade, `degraded` when a loop is failing, otherwise `healthy` | `/api/status`, joined with kube-state |
| Services: **Earn forecast worker** | Health of the `earn-synth` Deployment | kube-state |
| Services: **Earn nightly backup** | Age of the last successful `earn-backup` run. Overdue after 26 h | `kube_cronjob_status_last_successful_time` |
| Logs | Earn control-state changes, open safety stops, failed start-up checks, start-ups, failing loops, trip clears and rule acks. Routine order cancels are left out | `/api/status` |
| Storage | The `earn-data` volume | kubelet volume stats |
| Alerts | The `earn-alerts` rules in plain words | Prometheus |

Without `LIF_EARN_URL`, Services still shows Earn from kube-state alone. If earn is unreachable or rejects the key,
the Earn entry says so and Logs adds a note, and the LIF services and events are unaffected. Earn is not one of
the core services, so an Earn outage raises no console notification. Its own alerts cover that.

## 3. Experience per mode (§3)

| Mode | Default path | Screens | Who |
|---|---|---|---|
| **USE** | Open → see "Local AI Ready" → type → route → stream (§109) | Ask, compact Home, command bar, Agents → Run | Admin and paired devices |
| **OBSERVE** | Home answers §2 at a glance; drill down for detail | Home, System → Compute/Services, Jobs, Agents, Models (read) | Everyone signed in |
| **OPERATE** | System changes → Labzilla explains → shows impact → offers a safe action → user acts (§109) | Models (load/unload/promote/canary/rollback), Jobs (pause/resume/cancel), System → Settings, Connect (devices) | Admin; phones get safe operations only (§99) |

## 4. Mobile gateway and pairing

### Mobile gateway (§12, §13, §45, §66)

```
Labzilla ●                    🔔   ← top bar: logo, status dot, notifications
LABZILLA                            ← hero
● Local AI Ready · Local connection
[ Ask Labzilla…               ➤ ]  ← already on screen: tapping it is interaction 1
(Ask) (Code) (Research) (Summarize)  ← quick actions: light pills that wrap, not tiles
(Agent) (Upload) (Status)
Needs your attention (1 approval)   ← only when something is pending
Recent prompts (3 conversations)
──────────────────────────────────
Home   Ask   Agents   More         ← bottom nav
```

The prompt goes through the command resolver (`POST /api/command`) like the command bar on every other page:
"Pause batch jobs" gets its proposed action and "Why is the GPU busy?" its snapshot answer, in a sheet; plain
prompts continue to Ask. The field is one line, so pasting multi-line text (code) moves the draft into Ask's
composer instead, where the line breaks survive and the message can be finished before sending. The logo
appears twice on this screen (top bar and hero); that is a known, accepted repetition.

### Pairing flow (§15–§17)

| Step | Desktop (admin session) | Phone | Server |
|---|---|---|---|
| 1 | Connect → "Connect a device" | | `POST /api/pair/start` → single-use 32-byte token, expires after `pairing_ttl_sec` |
| 2 | Shows QR of `<public_url>/pair#<token>` and the URL | Scans QR, opens `/pair` | The token is in the URL fragment, so it never reaches server logs |
| 3 | | Enters a device name and confirms | `POST /api/pair/claim` → status `claimed`, 6-digit code, HttpOnly `lz_pair` claim cookie |
| 4 | Sees the same 6-digit code (SSE `pairing`) and approves | Shows the code and waits | `POST /api/pair/{id}/approve` (devices.manage) |
| 5 | Device appears in the list | Polls `GET /api/pair/status` and receives the device session | Device session cookie set, 90-day TTL |
| Revoke | Devices → Revoke | Next request gets 401 → sign-in screen | `DELETE /api/devices/{id}` deletes its sessions immediately |

Tokens are single-use, expire, and are rate-limited (10/min/IP). Same-device pairing is allowed.

## 5. Command bar (§7, §8, §49, §50)

Deterministic rules (`lif/console/intent.py`) classify first; a local-model fallback is not part of v1. The command bar never executes anything itself: operational commands return a `ProposedAction`, and the client runs it only after the user presses the button. Prompt text is never sent to Jev or any external service for classification.

| Example (spec) | Kind | Behaviour |
|---|---|---|
| "Ask the local model…" | `ai_prompt` | Hands the text to Ask (in memory, not in the URL) and streams there |
| "Write a Python parser for this JSON" (§8) | `ai_prompt` | Hands off to Ask with mode Code |
| "Run the code agent on repo X" | `agent_request` | Honest answer: no code agent is deployed. Offers Ask in Code mode and the list of runnable agents |
| "Check for better coding models" | `model_request` | Proposes "Check for better models: coding" (`POST /api/models/discovery`, models.discover), stating that it costs a few Jev calls |
| "Check whether any better coding models were released" (§8) | `model_request` | Same as above (model-discovery workflow) |
| "Check for better vision models" | `model_request` | Same, for vision models (`categories: ["vision"]`); its link opens Discovery with Vision ticked |
| "Why is the GPU busy?" | `system_query` | Answers from the poller snapshot: BLERBZ state and reason, memory split, what AI serving holds. Link to System → Compute |
| "Why did local/default fall back to local/fast?" (§8) | `system_query` | Answers from the role's `cause` / `cause_label` and fallback chain (for example, the default model was paused to free memory for BLERBZ). Link to the role |
| "Pause batch jobs" | `operational_command` | Proposes "Pause all batch jobs" (`POST /api/jobs/batch/pause-all`, jobs.control). Reversible, no confirmation, impact stated |
| "What changed today?" | `system_query` | Today's activity, grouped (models, jobs, system, decisions) |
| "Summarize current incidents" | `system_query` | Services that are not healthy, firing alerts, roles on fallback; "Nothing needs attention" when none |
| "Why are we using K3s?" (§33) | `knowledge_query` | Top knowledge hits with the decision record link |
| "Open GPU", "Open Settings" (§50) | `navigation` | Navigates directly |

The palette (Ctrl/Cmd+K) has the §50 entries (Ask Labzilla, Run Agent, Search Knowledge, Check Models, Open GPU, Pause Batch, Open Settings), navigation, and free text that goes through the same resolver. `/` focuses the command bar, or the page's own prompt or search box.

## 6. Health language (§38, §91)

| Health | Words | Color + icon | Typical cause |
|---|---|---|---|
| `healthy` | Healthy / Ready | green, check | Everything answering |
| `busy` | Busy | yellow, clock | BLERBZ generating, work queued, service starting |
| `degraded` | Degraded | yellow, warning | Serving from a fallback or a smaller model |
| `paused` | Paused | gray, pause ring | Paused by an operator or by the memory guard |
| `attention` | Needs attention | red, warning | Something needs a person |
| `offline` | Offline | red, dash | Not answering |
| `unknown` | Unknown | gray, question | Not measured yet or source unreachable |

The word follows the health: a model role below its quality floor says "Degraded" (not "Limited"), a role that isn't
answering says "Offline" (not "Unavailable"), and a finished agent run says "Finished" like a finished job (also the word for a successful event and a
finished timeline step). Counts are written out ("1 batch job", "3 batch jobs"), never "job(s)". When a command's
action button states its impact, the answer above it doesn't repeat it.

### Ask composer

```
┌──────────────────────────────────────────────────────────┐
│ What do you want to do?                                  │  grows with the text, then scrolls
│                                                          │
│ [🖼 1024×768 ✕]                                           │  image thumbnails, when attached
│ 📎  📷  🎙  Auto ▾  🔒 Local only                    [➤]  │  📷 on touch devices only
└──────────────────────────────────────────────────────────┘
🖼 Switched to Vision to read the image. Undo
(Why is the GPU busy?) (What changed today?) (Check for better local models) (Write a Python script…)
```

| Element | Behaviour |
|---|---|
| Mode (`Auto ▾`) | Native select with a screen-reader label "Mode"; the mode's blurb is its title. A line under the box appears only when capabilities couldn't be checked or the chosen mode is unavailable |
| Privacy chip | A switch named "Allow Jev routing": "Local only" (lock) or "Local + Jev". Its tooltip carries the per-mode description and the external-model note. With files attached it stays Local only, and the file list says so |
| Send | Icon button whose accessible name is "Send" (Stop while streaming). Desktop: Enter sends, Shift+Enter is a new line; touch: the button sends |
| Intent routing | In Auto mode, a short single-line prompt with no files goes through `POST /api/command` first. When the rules recognise a question or command about Labzilla ("Why is the GPU busy?", "Pause batch jobs", "Open GPU"), the answer appears inline under the prompt with "Ask the model instead"; nothing is stored in the conversation and no action runs until pressed. Requests to explain or teach ("Explain how GPU memory works"), multi-line or longer prompts, other modes and messages with files always go to the model. If the resolver can't be reached, the prompt goes to the model |
| Suggestions | Only while the prompt is empty. The three questions about Labzilla use the resolver whatever the mode; "Write a Python script…" fills the prompt in Code mode |
| Phones | Mode and privacy stay in the one-line summary chip that opens a sheet |
| Images (vision) | Attach, drag and drop, paste (an image wins over the URL text a copied image also carries), or 📷 (`accept="image/*" capture`). The browser downscales each image with a canvas to ≤ 1024 px on the longest side, JPEG q0.85 (EXIF rotation applied, white behind transparency); HEIC that the browser can't decode gets an honest note. At most 4 images per message, shown as thumbnails with Remove. In Auto, attaching an image switches the mode to Vision, said under the box with Undo (Auto also routes images to vision at the gateway). With no vision model installed, the box says "No local vision model is installed yet — Models › Check for better models (Vision)" (link to `/models/discovery?category=vision`) and Send stays off; a text-only mode offers "Use Vision" instead. An image alone is a valid prompt |
| History | Wide screens (> 1024 px): a persistent panel beside the conversation, collapsible ("Hide history"; the header's History button brings it back; remembered per browser). Tablets: a drawer; phones: a full-height sheet, one tap from the header. Rows: title, one-line preview, relative time or "Answering…" with a live dot (still under reduced motion), "from iPhone" when a paired device started it, selected state. Grouped Today / Yesterday / Earlier by the browser's calendar day. Search appears from 4 conversations. Keyboard: rows are links; ↑ ↓ Home End move, F2 renames, Delete deletes; ↓ from search enters the list. Rename is in place; Delete confirms (it removes the conversation from every device). Hovering or focusing a row warms its cache, so opening it shows the whole conversation at once, then refreshes |

### Translation table (raw term → console wording; the raw term stays in Technical Details)

| Raw term | Console says | Health |
|---|---|---|
| `CrashLoopBackOff` | Service repeatedly failed to start | attention |
| `ImagePullBackOff` / `ErrImagePull` | Couldn't download the service image | attention |
| Pod `Pending` | Waiting for resources | busy (attention if it persists) |
| `OOMKilled` | Ran out of memory and restarted | attention |
| Running, not Ready | Starting up | busy |
| `Evicted` | Stopped to free resources on the host | attention |
| Deployment scaled to 0 by the memory guard | Paused to free memory for BLERBZ | paused |
| gpusched `LOW` / `MODERATE` | BLERBZ idle (may start soon) | healthy |
| gpusched `HIGH` | BLERBZ likely within the hour; background work deferred | busy |
| gpusched `IMMINENT` (production lease) | BLERBZ is generating; AI requests slowed | busy |
| gpusched unreachable or stale | Can't see the GPU scheduler; running in safe mode | attention |
| Router fallback (chain index > 0) | Running on fallback model, with the cause in words | degraded |
| Served model below the role's quality floor | Smaller than this role expects | degraded |
| Batch reason `waiting: primary workload IMMINENT` | Waiting: GPU reserved for BLERBZ; resumes automatically | paused |
| Batch reason `batch paused by operator` | Paused by an operator | paused |
| Model `PRODUCTION` / `STANDBY` / `CANARY` | In use / Kept as backup / Trial: serving a share of traffic | healthy |
| Model `CANDIDATE` / `STAGED` / `BENCHMARKING` | Shortlisted / Downloaded, awaiting benchmark / Being evaluated | unknown / busy |
| Model `QUARANTINED` / `REJECTED` / `FAILED` | Blocked: checksum mismatch / Not good enough / Failed (download or test) | attention |
| Cascade route `auto` / `escalated` / `human` | Decided automatically / Decided by a stronger model / Waiting for your review | — |

### Fallback causes (`ModelRole.cause`)

| Cause | Label pattern |
|---|---|
| `shed_by_memory_guard` | "Default model paused to free memory for BLERBZ" |
| `yielded_to_primary` | "Slowed while BLERBZ is generating" |
| `primary_unavailable` | "Default model isn't answering; using the fast model" |
| `below_quality_floor` | "Serving a smaller model than this role expects" |
| `canary` | "Trying a candidate on a share of requests" |
| `not_deployed` | "No model is deployed for this role yet" |
| `unknown` | The raw reason, in Technical Details only |

## 7. Color semantics (§51, §57)

| Token | Dark | Meaning | Always paired with |
|---|---|---|---|
| `--brand` | `#00d878` | Brand, primary action, active nav. Used sparingly | Label |
| `--mint` | `#8ff0c4` | Focus ring, subtle highlight | — |
| `--success` | `#2fd17f` | Healthy, ready, done | Check icon + word |
| `--warning` | `#f2c14e` | Busy, degraded, waiting | Clock or warning icon + word |
| `--danger` | `#ff6b6b` | Failure, needs attention | Warning or error icon + word |
| `--info` | `#6cb6ff` | Information, reviews | Info icon + word |
| `--inactive` | `#7d8f8a` | Paused, unknown, queued | Ring or question icon + word |
| Surfaces | `#07110f` / `#0b1715` / `#10201d`, border `#1d302c` | Deep teal-navy from the logo | — |

Light theme equivalents meet WCAG AA. The theme follows the system preference; `<html data-theme>` overrides it (§52). Type is Inter variable (latin subset) with `cv05`, `cv08`, plus `zero` and `tnum` where numbers matter (§53).

## 8. Component inventory (§58, §85)

| Kind | Components (`apps/console/src/ui`) |
|---|---|
| Primitives | Button (consequence subtitle), IconButton, Input, Textarea, Select, Switch, Card, List/ListItem, Table (cards on compact), Modal, Dialog, Drawer, Sheet, Tabs, Badge, Tooltip, Progress, CodeBlock, Toast/Toaster, Skeleton, EmptyState, Icon, CommandInput, FactList, Markdown (safe markdown-lite) |
| Domain | StatusDot, StatusBadge, ResourceBar, ActivityRow, ApprovalCard, ModelCard, AgentCard, JobRow, DecisionBadge, RouteTrail, PrivacyBadge, TechDetails, ConfirmDialog, HumanErrorCard, Timeline |
| Shell (`src/shell`) | Layout, SideNav, BottomNav, TopBar, CommandBar, CommandPalette, CommandResult, Notifications, ConnectionBanner, useBreakpoint |
| Data (`src/api`) | `client.ts` (fetch, CSRF, HumanError, 401 → login, OfflineError, Ask stream), `sse.ts` (one EventSource, backoff), `store.ts` (`useResource` stale-while-revalidate), `session.ts`, `status.ts` |

Breakpoints are by available width, never user agent (§47): compact < 640 px, medium 640–1024 px, wide > 1024 px. Touch targets are at least 44 px on coarse pointers (§48).

## 9. Security model (brief §3; spec §16, §77, §78)

| Control | Implementation |
|---|---|
| First run | No users → `/setup`. Creating the admin requires the setup code from `secrets/lif-console-setup.code` (read on the host). Without a configured code, setup is refused |
| Passphrases | scrypt (n=2¹⁴, r=8, p=1), per-user salt, at least 10 characters |
| Sessions | Opaque 32-byte token in `lz_session` (HttpOnly, Secure, SameSite=Strict). Stored as sha256 with role, expiry and last seen. Logout deletes the row |
| CSRF | Non-GET requests need `X-Labzilla-CSRF` equal to the `lz_csrf` cookie, and Origin/Referer host equal to Host |
| Rate limits | Login 5/min/IP, 30/min overall, plus a per-name backoff (5 free failures, then 30 s doubling to at most 60 s). Setup 5/min/IP, 10/min overall. Pairing claims 10/min/IP, 30/min overall; the phone's status poll is limited per claim cookie, and a poll without one spends nothing. Every other mutation: 120/min per signed-in session (per IP only without a session); Ask messages 20/min per session, counted before the body is read, so a refused Ask costs no memory. Sign-in, setup, claim and sign-out don't spend the shared mutation budget |
| Known limits | Residual risks and their trade-offs are in CONSOLE_SECURITY *(private, local only)* |
| Request size | Bodies are capped before anything parses them: 4 KB for sign-in, setup, pairing and sign-out, 4 MB for an Ask message (512 KB prompt + 1 MB of file text, JSON-escaped; the gateway's own limit is 4 MB), 1 MB for Save to Knowledge, 64 KB elsewhere. Without a session every other route gets the 4 KB cap, and a bigger body gets the route's own 401 "Sign in to continue" unread (not a 413, so the page sends the person to sign in). If the session lookup itself hits a storage error, the answer is that 503. Over the cap when signed in: a human 413, nothing read. Chunked bodies are counted as they arrive. Ask bodies over 256 KB (or chunked) are read and parsed at most 3 at a time; one that waits more than 30 s for a turn gets a human 503 "Labzilla is busy taking other messages". Once an answer starts streaming, the raw request body is released. Login refuses names that can't exist before touching the backoff table |
| Storage | Ask keeps attachment text only while a turn can still be replayed as context (16,000 characters); bigger turns keep the file names. Images are never stored: a message keeps an image's name, size and dimensions, history shows "image not kept", and later turns replay it as text ("[Earlier image, not kept: name]"). An image is validated before anything is stored (JPEG, PNG or WebP by magic bytes, ≤ 1.5 MB decoded, ≤ 4 per message) and a request the gateway would refuse for size (> 4 MiB with its images) gets a human 413 first. Messages with images are sent Local only, like files. A conversation holds at most 64 MB of messages: past it, a new message gets a human 413 "This conversation is too long" (with New conversation) before anything is stored or sent. Each owner's conversations together are capped at 256 MB; past it the oldest conversations are removed (never the one in use). That pruning is best effort and runs after the turn is stored: if it fails, the answer still runs and the next check retries. Thread reads and writes that hit a locked or full database answer with a human 503, and a question is never stored without its answer row |
| Roles | admin: everything. device: read, ask, jobs.control, approvals.answer, models.discover, system.safe. Server-enforced on every route; a hidden button is never the control |
| Upstreams | Allowlisted calls only, no generic proxy. Keys (`LIF_CONSOLE_ADMIN_KEY`, `LIF_CONSOLE_GATEWAY_KEY`) stay server-side |
| Privacy | Ask defaults to "Local only" (CONFIDENTIAL). "Allow Jev routing" marks a prompt PUBLIC. A message with included files is always sent CONFIDENTIAL (Jev routing would see the file text), the composer and the file note say so, and the receipt reports what was actually used. External models are off by policy, and the UI says so |
| Headers | CSP `default-src 'self'` (no external origins), nosniff, `Referrer-Policy: same-origin`, `frame-ancestors 'none'`, `Cache-Control: no-store` on `/api` |
| Audit | Every mutation is recorded with the authenticated user (`auth.audit`) |
| Not in v1 | Passkeys (need a trusted secure context and a WebAuthn server library) |

## 10. Architecture (§75–§77)

```
phone / tablet / desktop browser
        │  HTTPS (Traefik ingress, host labzilla.<domain>)
        ▼
console  (lif.console.app, FastAPI, 1 replica, SQLite on its own PVC)
  ├─ static PWA (apps/console/dist) + SPA fallback, service worker for the app shell only
  ├─ /api/*            auth · sessions · roles · CSRF · rate limits · pairing · audit   (auth.py, routes/auth.py)
  ├─ /api/events       SSE hub: one poller → every viewer                          (events.py, poller.py)
  ├─ /api/system|models|jobs|agents|approvals|knowledge|ai|command                 (routes/*.py)
  └─ upstream.py (allowlist, server-side keys, per-call timeouts)
        ├─ controller   /v1/overview /v1/gpu /v1/models /v1/routing /v1/aliases /v1/discovery/runs
        │               /v1/benchmarks /v1/activity /v1/settings /v1/availability /v1/storage /v1/de/*
        ├─ gateway      /v1/health /v1/capabilities /v1/chat/completions (stream)
        ├─ batch        /v1/batch (jobs, stats, pause/resume/cancel)
        ├─ prometheus   instant + query_range (timeline, memory split, alerts)
        └─ knowledge    LIF_KNOWLEDGE_URL, else in-process read-only lif.knowledge over bundled public repos
```

| Module | Owner role | Purpose |
|---|---|---|
| `contracts.py`, `gen_ts.py` | ARCH | Domain models (single source of truth) and the TypeScript generator |
| `settings.py`, `errors.py` | ARCH | Env/config/secret accessors (read at call time); `{error: HumanError}` bodies |
| `app.py`, `db.py`, `auth.py`, `events.py`, `routes/auth.py` | CORE | App, SQLite, sessions/roles/CSRF/audit, SSE hub, identity routes |
| `upstream.py`, `humanize.py`, `poller.py`, `routes/system.py`, `routes/models.py` | SYS | Upstream clients, raw → human translation, snapshot cache, system and model routes |
| `intent.py`, `routes/ai.py`, `routes/jobs.py`, `routes/agents.py`, `routes/knowledge.py` | AI | Command classifier, Ask streaming, jobs, agents and approvals, knowledge |

No new Python dependencies: `hashlib.scrypt`, `secrets`, stdlib `sqlite3` (WAL), and `StreamingResponse` for SSE.

## 11. API surface (all JSON under `/api`; errors are `{"error": HumanError}`)

| Area | Endpoint | Returns | Permission |
|---|---|---|---|
| auth | `GET /api/auth/me` | `User` (401 when signed out) | — |
| auth | `POST /api/auth/login`, `POST /api/auth/logout` | `User` / `OkResponse` | — |
| setup | `GET /api/setup`, `POST /api/setup` | `SetupState` / `User` | setup code |
| access | `GET /api/access` | `AccessInfo` | read |
| pairing | `POST /api/pair/start`, `GET /api/pair/{id}`, `POST /api/pair/{id}/approve\|reject` | `Pairing` | devices.manage |
| pairing | `POST /api/pair/claim`, `GET /api/pair/status` | `Pairing` | token + claim cookie |
| devices | `GET /api/devices`, `DELETE /api/devices/{id}` | `Device[]` / `OkResponse` | read / devices.manage |
| events | `GET /api/events` | SSE: `status`, `activity`, `jobs`, `approval`, `notification`, `thread`, `pairing`, `model`. `thread` goes to every session of the owner (desktop and paired phones share the user id; never another user): kind `message` (partial answer, ~1 s), `upsert` (`ThreadSummary` after create, new prompt, rename, answer started or finished), `deleted` (deleted on a device, or removed by the storage cap) | read |
| system | `GET /api/system/status` | `SystemStatus` (from cache, < 200 ms) | read |
| system | `GET /api/system/compute` | `ComputeView` | read |
| system | `GET /api/system/timeline?hours=12` | `TimelineResponse` | read |
| system | `GET /api/system/services`, `POST /api/system/services/{key}/retry` | `ServiceHealth[]` / `ServiceHealth` | read / system.safe |
| system | `GET /api/system/storage`, `/logs?level=&q=`, `/alerts` | `StorageSummary`, `LogsResponse`, `AlertsResponse` | read |
| system | `GET /api/system/settings`, `POST /api/system/settings` | `SettingsView` | read / system.settings (jobs.control may set `batch_paused`) |
| models | `GET /api/models` | `ModelsOverview` | read |
| models | `GET /api/models/roles/{role}`, `POST /api/models/roles/{role}/rollback` | `ModelRole` / `OkResponse` | read / models.release (typed confirm) |
| models | `GET /api/models/deployments/{id}` | `DeploymentDetail` | read |
| models | `GET /api/models/deployments/{id}/preview?action=` | `ActionPreview` | read |
| models | `POST /api/models/deployments/{id}/{action}` | `OkResponse` | load/unload/benchmark/download/test: models.operate; promote/canary/delete/block/unblock/pin/unpin: models.release |
| models | `GET /api/models/discovery`, `POST /api/models/discovery` | `DiscoveryState` | read / models.discover |
| models | `GET /api/models/candidates`, `GET /api/models/candidates/{id}/compare` | `CandidateItem[]` / `CandidateComparison` | read |
| ai | `GET /api/ai/capabilities` | `AiCapabilities` | read |
| ai | `GET/POST /api/ai/threads`, `GET/DELETE /api/ai/threads/{id}` | `ThreadSummary[]` / `Thread` | ask |
| ai | `PATCH /api/ai/threads/{id}` (`{title}`, ≤ 120 characters; doesn't reorder the list) | `ThreadSummary` | ask |
| ai | `POST /api/ai/threads/{id}/messages` | SSE: `route`, `phase` (waiting → reading, with prompt-token progress), `delta`, `receipt`, `error`, `done` | ask |
| ai | `POST /api/ai/threads/{id}/messages/{mid}/cancel` | `OkResponse` | ask |
| command | `POST /api/command` | `CommandResolution` | read |
| jobs | `GET /api/jobs?status=`, `GET /api/jobs/{id}` | `JobsResponse` / `Job` | read |
| jobs | `POST /api/jobs/{id}/pause\|resume\|cancel`, `POST /api/jobs/batch/pause-all` | `Job` / `OkResponse` | jobs.control |
| agents | `GET /api/agents`, `GET /api/agents/{id}`, `POST /api/agents/run` | `AgentsOverview` / `AgentRun` | read / models.discover or models.operate |
| approvals | `GET /api/approvals`, `POST /api/approvals/{id}` | `Approval[]` / `Approval` | read / approvals.answer (pairings: devices.manage) |
| knowledge | `GET /api/knowledge`, `/search?q=`, `/objects/{key}` | `KnowledgeHome`, `KnowledgeHit[]`, `KnowledgeObjectResponse` | read |
| knowledge | `POST /api/knowledge/notes` | `OkResponse` | admin; refused while knowledge is read-only |
| — | `GET /healthz`, `/readyz`, `/metrics` | liveness, readiness, Prometheus | none |

There is deliberately no `use-fallback` endpoint: no backend can switch a role to its fallback on demand.

## 12. Performance budgets (§92, §93)

| Budget | Target | How |
|---|---|---|
| Initial JS (shell + Home + Ask) | ≤ 60 KB gzip | Preact, no UI framework, no chart or icon library; every other route is a lazy chunk |
| Ask route total | ≤ 75 KB gzip | Markdown-lite renderer, no syntax highlighter |
| CSS | ≤ 20 KB gzip | Tokens + one kit stylesheet |
| Fonts | Inter variable, latin subset only | One woff2 file, `font-display: swap` |
| `GET /api/system/status` | < 200 ms | Served from the poller cache, never waits on an upstream |
| Live updates | Server pushes over SSE | Upstreams polled once per `poll_sec` (default 5 s) for all viewers |
| Pod | requests 50m CPU / 96 Mi, limits 1 CPU / 320 Mi | The primary workload owns the host memory |

Budgets are build-time targets (`vite build` reports compressed sizes). This document carries no production measurements.

## 13. Coverage matrix (§1–§111)

The status is the v1 target agreed in the architecture brief, and the integration phase verifies it. **Done**: the spec intent is met. **Partial**: met within an upstream limit, as noted. **Deferred**: not in v1, for the reason given.

| § | Topic | Status | Where | Note |
|---|---|---|---|---|
| 1 | See, understand, use, control, review, intervene | Partial | Whole console | Review and intervention are limited by the backends: review tickets are after-the-fact labels (§40), and there is no use-fallback (§82) |
| 2 | Always answerable: what, why, wrong, next | Done | Home status block, headline, notifications, command bar | |
| 3 | USE / OBSERVE / OPERATE modes | Done | Ask + Mobile Gateway / Home + System / Models, Jobs, Settings | OPERATE needs admin; phones get safe operations |
| 4 | Minimum information | Done | Design rule; Home = status block + cockpit (§6) | Changed 2026-10-01: the owner asked for an executive cockpit |
| 5 | Compact left nav; technical areas contextual | Done | `shell/SideNav`, `shell/nav.ts` | See the table in section 2 |
| 6 | Home command center | Done | `pages/home`, `GET /api/home` | Status block (eager), then the cockpit chunk (`Cockpit.tsx`, ~4.5 KB gz): KPI tiles (Ask answers, answer speed, first word p90, availability, free memory with the 8 GB margin, GPU load, AI requests, value estimate) with 6-hour inline-SVG trends where no data is a gap, never 0; recent conversations; attention full-width when something needs the owner, otherwise "Nothing needs you"; services; activity with repeats folded ("· 8×"). `/api/home` refreshes every 60 s while visible and on `thread` events; its shared upstream parts are cached (trends 60 s, value 120 s) and each part degrades with its own reason. "Agents running" counts synthesized runs (§22). Every resource row (GPU load, memory, BLERBZ) drills down to System → Compute and says when its data is out of date. Live state (who answers, GPU owner, reviews) is not shown once its upstream has been silent for three of its poll intervals (at least 30 s) |
| 7 | Persistent command bar | Done | `shell/CommandBar`, `POST /api/command` | Desktop: bottom of the content column. Phone: above the bottom nav. On phone Home the gateway prompt replaces it and goes through the same resolver |
| 8 | Command kinds | Partial | `intent.py` | Rules cover every §7/§8 example, in the command bar and in Ask's composer (Auto mode, short prompts). Other text falls back to "Ask Labzilla". No local-model classifier in v1 |
| 9 | Ask screen with route, privacy, receipt | Done | `pages/ask`, `routes/ai.py` | Latency is measured by the console. The gateway's streaming metadata has no latency |
| 10 | Logical capabilities, not physical models | Partial | Ask mode picker, `AiCapabilities` | Vision works once a local vision model is installed; until then the composer says so and links to a vision check. "Local only" is the privacy choice. "View physical model" is in the receipt |
| 11 | Routing transparency | Partial | `RouteTrail`, `Receipt.route` | Kimi K3 never appears (external models are off). For `local/auto`, the resolved model is known only when the answer finishes |
| 12 | Mobile Gateway first-class | Done | Compact Home | |
| 13 | Fewer than 2 interactions before typing | Done | Prompt already visible on compact Home | Pasting multi-line text there continues in Ask's composer (one-line field) |
| 14 | Friendly local address | Done | `/api/access`, Trust page | `https://labzilla.local` (mDNS, published by a user unit) is the primary address; `labzilla.tiny-dgx.lan` needs LAN DNS or `/etc/hosts` |
| 15 | Connect Mobile: QR + URL | Done | `pages/connect`, "Connect a phone" in the side nav (admin) | qrcode-generator is lazy-loaded on this page only. Phones reach it from More |
| 16 | Secure, low-friction LAN auth | Partial | `auth.py`, pairing | Pairing, short-lived tokens and sessions are in v1. Passkeys are deferred (they need trusted HTTPS and a WebAuthn library) |
| 17 | Pairing flow and revocation | Done | `routes/auth.py`, Connect, Pair | |
| 18 | Direct prompt URL `/ask` | Done | Route `/ask` | |
| 19 | Voice input | Partial | Ask composer | Web Speech API is feature-detected. It needs a secure context and browser support, and is hidden with a reason otherwise |
| 20 | File intake, "Processing: Local only" | Partial | Ask composer, `AttachmentIn` | Text, code, logs, markdown, JSON and CSV (≤ 256 KB) are read in the browser. Images are downscaled in the browser and sent to the local vision model, never stored. PDFs are answered honestly: no PDF text extraction yet. A message with files or images stays Local only even when Jev routing is on |
| 21 | Response actions | Partial | Ask | Copy, Continue and Open Details are in v1. Save to Knowledge is not offered as a write yet: the console reads knowledge only. Run as Agent is hidden: no installed agent takes a free-form task yet (Model Scout and the Evaluator run fixed model checks) |
| 22 | Agents screen | Partial | `pages/agents`, `routes/agents.py` | There is no AgentRun backend. Runs are synthesized from discovery runs, benchmark activity, the decision cycle and traces, and labelled by source. There is no Code Agent |
| 23 | Agent detail | Partial | `/agents/:id` | Goal, steps, decisions, tools and artifacts come from the source records. Cost is shown only where Jev spend is recorded. No chain-of-thought |
| 24 | Activity timeline | Done | `Timeline` | Built from activity events |
| 25 | Jev decision attribution | Partial | `DecisionBadge` in receipts and agent runs | Shown when the route decision is known. Streaming responses don't always carry it |
| 26 | Models portfolio by role | Done | `pages/models` | |
| 27 | Model detail and actions | Partial | `/models/deployments/:id` | Memory is configured or estimated: no per-model measurement exists. Speed is labelled measured, estimated or live. Dangerous actions are previewed. Test is not available (requests are routed by role, so one model can't be targeted; use Benchmark). Unload and Rollback are disabled with the reason when the server would refuse (a role uses the model; no earlier version) |
| 28 | Discovery with stages | Partial | `/models/discovery` | Upstream has no intra-run progress: the stage counts appear when the run finishes. "What to look for" picks categories, including Vision (`?category=vision` preselects); Models offers "Find a vision model" while none is installed. A role with no model yet (the first vision model) offers Promote instead of a trial |
| 29 | Candidate comparison | Partial | `/models/candidates/:id` | Stability comes from benchmark errors only; no canary outcome metrics exist. "Ignore" hides the candidate on this device only (there is no backend ignore; blocking is an API-only release action in v1) |
| 30 | Jobs combine all background work | Partial | `pages/jobs`, `routes/jobs.py` | Batch jobs are durable. Discovery, download and benchmark tasks are in controller memory and lost on restart. Scheduled work is static descriptors |
| 31 | Resource-aware reasons | Done | `humanize.job_state` | "Paused — GPU reserved for BLERBZ — resumes automatically" |
| 32 | Knowledge as project memory | Partial | `pages/knowledge` | Read from the knowledge service (deployed 2026-10-01); the console shows public objects only and never the private repo. Recent changes come from object dates |
| 33 | Single knowledge search | Partial | `/api/knowledge/search` | Public objects only. Incidents live in private knowledge, so "the last OOM incident" gets an honest "no public record" |
| 34 | Decision record page | Done | `/knowledge/o/:key` | History is limited to what the object records. A record whose `selected` is a bare option token shows its title as the decision, with the token in brackets |
| 35 | System consolidates infrastructure | Partial | `pages/system` | Tabs: Compute, Services, Storage, Network, Logs, Settings. Kubernetes is folded into Services, because RBAC cannot read ai-system pods |
| 36 | Compute view | Partial | System → Compute, `ResourceBar` | GPU %, unified memory and the BLERBZ/AI/other split come from gpusched and Prometheus. Temperature: no GPU sensor is exported, so it shows "Not measured" |
| 37 | Workload timeline | Done | `/api/system/timeline` | Prometheus `query_range`, whole clock hours. The controller's availability probes don't count as inference. An honest empty state when Prometheus is down |
| 38 | Health language | Done | `humanize.py`, `StatusBadge` | Firing alerts are titled in words; the alert name is in Technical Details |
| 39 | Notify only when action may be needed | Done | `Notification.kind` is limited to the §39/§100 list | |
| 40 | Approval UX with consequences | Partial | `ApprovalCard`, `/api/approvals` | Device pairings are real approvals. Decision review tickets block nothing upstream, so they show "Nothing is waiting on this answer" |
| 41 | Strong confirmation for dangerous ops only | Done | `ConfirmDialog` (typed for rollback) | Delete, block and pin are API-only in v1 (the typed delete preview exists server-side). Maintenance mode is reversible and confirms nowhere, in Settings or from the command bar |
| 42 | Progressive disclosure | Done | `TechDetails` | |
| 43 | Platform-neutral web design | Done | Native controls, no imitation chrome | |
| 44 | Desktop layout | Done | `shell/Layout` | |
| 45 | Mobile layout | Done | `TopBar` compact, `BottomNav` | |
| 46 | Tablet | Done | Medium breakpoint: icon nav, touch sizes | |
| 47 | Behavioural breakpoints | Done | `useBreakpoint` (width, never user agent) | |
| 48 | Touch targets | Done | 44 px on coarse pointers | |
| 49 | Keyboard-first | Done | Ctrl/Cmd+K, `/`, focus rings, skip link | |
| 50 | Command palette | Done | `shell/CommandPalette` | |
| 51 | Visual language from the logo | Done | `styles/tokens.css` | |
| 52 | Dark primary, light provided | Done | Tokens + `data-theme` | |
| 53 | One legible sans | Done | Inter variable (latin), `cv05`, `cv08`, `zero`, `tnum` | |
| 54 | Density levels | Partial | `.density-normal`, `.density-compact` | No "dense" level (never a default anyway) |
| 55 | Tables only where comparison matters; cards on phones | Done | `Table` (`compactCards`) | |
| 56 | Charts only for trends | Done | Workload timeline only; no chart library | |
| 57 | Color semantics, never color alone | Done | Tone + icon + word everywhere | |
| 58 | Domain components | Done | `src/ui` | |
| 59 | Motion only for state | Done | Streaming caret, progress, live dot; off under reduced motion | |
| 60 | Feels immediate | Done | `useResource` stale-while-revalidate, skeletons, streaming | Pause all batch jobs and per-job pause/resume update before the server answers and revert on refusal. A refresh already in flight when something changes is not trusted: it re-runs |
| 61 | Live updates over SSE | Done | `/api/events` | Upstreams have no push, so the server polls once and fans out |
| 62 | Offline state | Done | `ConnectionBanner`, `OfflineError` | |
| 63 | "● Local connection" indicator | Done | Side nav footer (desktop, tablet), Mobile Home hero, top-bar lock icon on other phone pages | |
| 64 | Per-request privacy indicator | Done | `PrivacyBadge`, `Receipt.privacy` | |
| 65 | External-model consent sheet | Deferred | Component only | Never shown: external routing is disabled by privacy policy |
| 66 | Mobile shortcuts | Partial | Mobile Gateway | Ask, Code, Research (Deep), Summarize, Agent, Upload, Status. Customization later |
| 67 | Lightweight prompt history | Done | Ask history panel (wide) / sheet (phone): Today / Yesterday / Earlier | Grouped by the browser's calendar day (the server runs in UTC). Live on every open console; rename, delete (confirmed), search. "What changed today?" answers for the last 24 hours with relative times |
| 68–70 | Cross-device sessions, server-side state | Done | Threads in SQLite, `thread` SSE events | An answer keeps streaming server-side if the phone disconnects. Every console of the owner sees new conversations, prompts and partial answers live, and an open conversation deleted elsewhere is left with a note. After a dropped or slept connection the console reconnects and refetches. Outside Ask (or on a tablet), a desktop sees "Conversation started on another device · Open" once per conversation it didn't start; the phone's Continue sheet shows a QR code and the link |
| 71 | PWA | Partial | `manifest.webmanifest`, `sw.js` (app shell only) | Install and the service worker need trusted HTTPS; see Deploy & access |
| 72 | Optional desktop install | Partial | Same as §71 | Never required |
| 73 | No Electron | Done | | |
| 74 | mDNS / `.local` | Done | `homelab/host/systemd/labzilla-mdns.service` | Published via `avahi-publish` as a user unit (no sudo). There is no bare-IP fallback: the ingress routes by host name, so `https://<LAN-IP>/` returns 404 |
| 75 | Architecture: one console in front of internal services | Done | Section 10 | |
| 76 | One stable endpoint | Done | `/api/*` | |
| 77 | Gateway owns auth, sessions, CSRF, rate limits, pairing | Done | `auth.py` | |
| 78 | Trusted HTTPS, explicit fallback | Done | `homelab/networking/local-ca`, Trust page | Name-constrained local CA serves every LAN host (2026-10-01). Each device must trust the CA once; until then the fallback is explicit, never silent |
| 79 | Technical Details drawer | Done | `TechDetails` | |
| 80 | Logs: errors, warnings, relevant events first | Partial | System → Logs | Built from controller activity. Raw logs are not available (no RBAC for ai-system pod logs), and the page says so |
| 81 | Errors: what failed, impact, next step | Done | `HumanError`, `HumanErrorCard`, `errors.py` | |
| 82 | Recovery actions | Partial | Retry (re-probe), Rollback (admin) | No restart or use-fallback backend exists, so neither is offered. Never "run kubectl" |
| 83 | First-run setup | Done | `/setup` | |
| 84 | "Labzilla is ready" | Done | Setup final step | |
| 85 | Primitives | Done | `src/ui` | |
| 86 | Accessibility | Done | Semantic HTML, ARIA where needed, focus management | Checked by axe in e2e |
| 87 | Framework choice | Done | Preact + TypeScript + Vite | The Control Center is a single static page. Next.js was rejected: a Node pod costs RAM the primary workload needs |
| 88 | Server rendering where it helps | Deferred | | Static SPA: the shell is small and cached. SSR would need a Node server |
| 89 | No global mega-store | Done | `store.ts`, URL state, SSE | |
| 90 | Typed contracts | Done | `contracts.py` → `contracts.gen.ts` | |
| 91 | Translate k8s terms | Done | `humanize.py` | |
| 92 | Desktop performance | Done | Section 12 | |
| 93 | Mobile performance | Done | Section 12 | |
| 94 | Logo used sparingly | Done | Sign-in, side nav header, mobile header | |
| 95 | Empty states with actions | Done | `EmptyState` | |
| 96 | Contextual guidance | Done | Role blurbs, button subtitles | |
| 97 | Optional advanced controls | Partial | Technical Details, System → Settings | Physical models and k8s details are shown. Routing thresholds, decision schemas and GPU reservations are read-only or absent |
| 98 | Explain before dangerous ops | Done | `ActionPreview` from the console | Upstream has no dry-run; the console computes the preview |
| 99 | Safe operations on mobile | Partial | Device role permissions | Pause a job, answer a review, re-probe a service, view health. Restarting a service and switching to fallback have no backend |
| 100 | Mobile notifications | Partial | In-app notifications | Web Push is not in v1: it needs trusted HTTPS and a push service |
| 101–102 | Cross-browser and size validation | Partial | Playwright + axe at compact, medium and wide (Chromium) | Chromium only so far: Safari, Firefox, Edge and real phones are not yet tested (owner, by hand) |
| 103 | First-time tasks without instructions | Partial | All ten paths exist | "Run an agent" is limited to Model Scout and Evaluator |
| 104 | Phone: review a code snippet | Done | Mobile Gateway → Ask (Code) | |
| 105 | Newly paired device end to end | Done | Pair → Ask → "Open on desktop" | |
| 106–107 | UX metrics | Deferred | | No telemetry pipeline in v1. The audit log records actions, and `/metrics` has request counters only |
| 108 | Feel: fast, calm, trustworthy | Done | Design intent | |
| 109 | Default and operations flows | Done | Sections 3 and 5 | |
| 110 | 5-second comprehension; know before acting | Done | Principles | |
| 111 | Make the system feel simpler than it is | Done | | |

## 14. Follow-ups (not in v1)

| Item | Needs |
|---|---|
| Passkeys | Trusted HTTPS on every device + a WebAuthn server library |
| Durable controller jobs (discovery, download, benchmark) with progress and cancel | Controller jobs table |
| Use-fallback and restart-service recovery | Controller endpoints with an approval flow |
| Gated approvals (agent waits for a person) | A decision-fabric ticket link back to the waiting caller |
| GPU temperature | DCGM or a gpusched metric |
| Raw pod logs in System → Logs | RBAC for ai-system pod logs through the controller |
| Web Push for §100 | Trusted HTTPS + a local push design (no external push provider) |
| Local-model command classification | A CONFIDENTIAL decision package on `local/instant` |

## Deploy & access

**Status: deployed 2026-10-01** (image tag `*-console`; console + knowledge service, mDNS name and local CA in place). First-run setup (step 5) and trusting the CA on each device remain the owner's.

### What ships

| Piece | Where | Notes |
|---|---|---|
| Image | `Dockerfile` (stage `ui`, `node:24.16.0-bookworm-slim`) | `npm ci && npm run build` (tsc + vite); the final stage copies `dist/` to `/app/apps/console/dist`. Same `lif/fabric` image as every LIF service: no Node at runtime |
| Knowledge (read-only) | `COPY knowledge` → `/app/knowledge` | Public repos only (`data_class: PUBLIC`). The private repo lives outside `lif/` and is never in the build context; `.dockerignore` drops the derived index |
| Deployment `console` | `deploy/k8s/base/16-console.yaml` | 1 replica, `Recreate` (one SQLite writer). Priority `ai-interactive`: user-facing but not on the inference path, so below `ai-critical`. Requests 50m / 96 Mi, limits 1 CPU / 320 Mi. Read-only root, `/tmp` emptyDir, no service-account token |
| Volume `lif-console` | same file | Longhorn 1 Gi RWO, `/data/console.db` (users, sessions, devices, threads, audit). In Longhorn's `default` recurring-backup group like every unlabelled volume |
| Secrets | `ai-system/lif-secrets` keys `LIF_CONSOLE_ADMIN_KEY`, `LIF_CONSOLE_GATEWAY_KEY`, `LIF_CONSOLE_SETUP_CODE` | Mounted as the only three items (no Jev or inter-service keys), `optional`: before they exist the console starts and shows "Not configured" |
| Service `console:8080` (port `http`) | same file | Label `app.kubernetes.io/part-of: lif`, so the `lif-services` ServiceMonitor scrapes `/metrics` |
| NetworkPolicy | `12-networkpolicy.yaml` | `console-ingress` admits only Traefik (`kube-system`, `app.kubernetes.io/name: traefik`), Prometheus (`monitoring`, `app.kubernetes.io/name: prometheus`) and the host (kubelet probes), because the console believes forwarded headers from the pod network. `console` joins the `internal-services` callers (console → batch for per-job pause/resume/cancel). Controller and gateway already admit all of `ai-system` |
| Ingress | `13-ingress.yaml` | `labzilla.tiny-dgx.lan` and `labzilla.local` → `console:8080`, same Traefik `websecure` entrypoint and TLS as the other hosts |
| Config | `config/lif.yaml` → `console:` | `public_url` (QR codes and Connect Mobile use it), `alt_urls`, session and pairing TTLs, `poll_sec`, `default_mode` |

### Owner steps

Steps 1–4 and the local CA (step 7) were done on 2026-10-01. To start using the console on a new device, follow the repo [README → Use Labzilla](../../README.md#use-labzilla-the-console). The steps below are for a rebuild from scratch.

1. **Create the console secrets.** `~/labzilla/lif/scripts/create-secrets.sh` (idempotent, never prints values). It generates `secrets/lif-console-admin.key`, `secrets/lif-console-gateway.key` and `secrets/lif-console-setup.code` once (0600, git-ignored), adds `console:<key>` lines to `LIF_ADMIN_KEYS` and `LIF_GATEWAY_KEYS`, and adds the three `LIF_CONSOLE_*` keys to `lif-secrets`.
2. **Roll out gateway and controller.** They read their key lists only at startup, so the console's keys are refused until they restart:
   ```bash
   kubectl -n ai-system rollout restart deploy/gateway deploy/controller
   kubectl -n ai-system rollout status deploy/gateway && kubectl -n ai-system rollout status deploy/controller
   ```
   The gateway (2 replicas + PDB) rolls without downtime. The controller is `Recreate`, so the Control Center and admin API are briefly unavailable. Skip this step when step 3 follows straight away: a new image tag rolls every LIF Deployment anyway.
3. **Build, push and apply** exactly as in [DEPLOYMENT.md §2](DEPLOYMENT.md#2-build-and-deploy) (`kubectl diff` first). The `ui` stage needs network access to the npm registry during `docker build`. Then:
   ```bash
   kubectl -n ai-system rollout status deploy/console
   ```
   Run DEPLOYMENT.md's "After every apply" headroom check: the console adds at most 320 Mi.
4. **Make the name resolve** on each client (see *Name resolution* below). Until then, a desktop can test with `curl -sk --resolve labzilla.tiny-dgx.lan:443:<LAN-IP> https://labzilla.tiny-dgx.lan/healthz`; `<LAN-IP>` is the node address used in [OPERATIONS.md → Access](OPERATIONS.md#access).
5. **First run.** Open `https://labzilla.local`; with no users it goes to **Setup**. On the host, the owner reads the setup code with `cat ~/labzilla/secrets/lif-console-setup.code` (agents never read it) and types it into the setup form with an admin name and a passphrase of at least 10 characters. Without the code, setup is refused.
6. **Connect a phone.** Desktop → **Connect a phone** (side nav) shows a QR code for `<public_url>/pair#<token>` (single use, 120 s). The phone opens it, both screens show the same 6-digit code, and the desktop approves. Revoke under **Connect → Devices**.
7. **Optional: trusted HTTPS** (see *TLS* below) to unlock install, voice and notifications.
8. **Real client addresses (committed, takes effect on push).** `homelab/networking/metallb/config/traefik-ip.yaml` sets `externalTrafficPolicy: Local` on the Traefik Service, so per-IP limits see each client's own address. Argo CD applies it when `main` is pushed; Traefik restarts once (a brief interruption of every LAN ingress). Rationale: CONSOLE_SECURITY *(private, local only)*.

### Name resolution

| Option | How | Reaches phones | Effort |
|---|---|---|---|
| **LAN DNS (recommended)** | On the router or LAN DNS server: an A record for `labzilla.tiny-dgx.lan` (or `*.tiny-dgx.lan`, which also covers `lif.`/`ai.`) → `<LAN-IP>` | Yes, every device on the LAN | One record; no host change |
| `/etc/hosts` | Add `labzilla.tiny-dgx.lan` to the existing `<LAN-IP>` line from OPERATIONS.md → Access | Desktops only (phones can't edit hosts) | Per client |
| **mDNS `labzilla.local` (in place)** | User unit `homelab/host/systemd/labzilla-mdns.service` runs `avahi-publish -a -R labzilla.local <LAN-IP>` (no sudo; linger keeps it running). Install steps are in the unit file | Apple and Linux resolve `.local` reliably; Android and Windows depend on version, so check each device | Done |

The QR code and Connect Mobile use `console.public_url`, which is `https://labzilla.local`. There is no bare-IP fallback: the ingress routes by host name only.

### TLS

Option 1 is in place (2026-10-01): `homelab/networking/local-ca/issue-certs.sh` created a local CA, name-constrained to `tiny-dgx.lan` and `labzilla.local`, and issued one server certificate for all four LAN hosts. It is Traefik's default through the `default` TLSStore. **Renew** by rerunning the script before 2027-10-03; the certificate expires 2027-11-02, and the script reissues it when fewer than 30 days remain. **Each device trusts `secrets/labzilla-ca.crt` once** (the CA certificate is public; never copy the `.key`). Until a device trusts it, that device behaves as in option 3. The console never falls back to plain HTTP: session cookies are `Secure`, and `LIF_CONSOLE_INSECURE_COOKIES` is for local development only.

| Option | How | Gains | Costs |
|---|---|---|---|
| **1. Local CA (in place)** | `issue-certs.sh` creates a small local CA on the host (the CA key stays in `secrets/`). It issues one cert for `labzilla.tiny-dgx.lan`, `labzilla.local`, `lif.tiny-dgx.lan` and `ai.tiny-dgx.lan`, store it as a TLS Secret in `kube-system`, and make it Traefik's default with a `TLSStore` named `default` (`traefik.io/v1alpha1`, `spec.defaultCertificate.secretName`). Install the root on each device: iOS (profile, then *Certificate Trust Settings*), Android (*Install a certificate → CA certificate*), desktops (system or browser trust store) | Full PWA: install, offline shell, voice, notifications, clipboard. Works with `.local` (public CAs never issue `.local`) | Install the root on every device once; renew the leaf before it expires |
| 2. Tailscale `ts.net` | A Tailscale Ingress for `console` (as `homelab/networking/tailscale/ingresses/grafana.yaml` does), plus the Tailscale proxy pods in the `console-ingress` NetworkPolicy (and in `console.trusted_proxies` if they sit outside the pod network), and `public_url` set to `https://labzilla.<tailnet>.ts.net` | A publicly trusted cert with no device setup; reachable from anywhere on the tailnet | Only devices signed in to the tailnet; a second access path to keep in mind |
| 3. Untrusted (devices without the CA) | Accept the browser warning once per device | Sign-in, Ask streaming, pairing, live updates | No install, no offline shell, and voice/notifications are unavailable or unreliable. The warning returns on some browsers |

What works without a trusted certificate (option 3). Features are detected in the browser and never silently disabled; the **Trust this device** page says which are off and why. A clicked-through certificate still counts as a secure context, so the browser may offer voice and notifications; when the service worker can't register there, Trust marks them "May be blocked" rather than available.

| Works | Needs a trusted certificate |
|---|---|
| Sign in, sessions, CSRF protection | Install as an app (PWA) and the offline app shell (service worker) |
| Ask, with streaming and receipts | Voice input |
| Pairing a phone by QR (after accepting the warning on the phone) | Notifications |
| Live status, jobs, approvals and model actions | Clipboard copy on some browsers |
| Knowledge search, System pages | Passkeys (not in v1 anyway) |

### Rollback

Nothing depends on the console: the gateway, controller, batch and Control Center never call it. To take it out, `kubectl -n ai-system delete deploy/console` (the `labzilla.*` hosts then return 503 from Traefik; everything else is unaffected), and remove `16-console.yaml` from `kustomization.yaml` so the next apply doesn't bring it back. The `lif-console` volume keeps users, devices and threads; delete it only to erase them (`kubectl -n ai-system delete pvc lif-console`, irreversible). To withdraw the console's upstream access too, drop its `console:` lines from `LIF_ADMIN_KEYS` and `LIF_GATEWAY_KEYS` in `create-secrets.sh`, re-run it, and roll out the gateway and controller.
