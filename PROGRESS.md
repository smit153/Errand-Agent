# Progress

**Build in progress (started 2026-11-07, user-approved).** Source of truth: `Orange_Engineering_Assignment.pdf` (v2 calls it "ASSIGNMENT.md (the PDF)"; no separate ASSIGNMENT.md file is in the repo). `docs/DESIGN_REVISION_v2.md` was saved from the user's paste and cross-checked: every v2 point is in docs/DECISIONS.md §§1–16 / DESIGN.md. Build plan: DECISIONS.md §15. Stages 1–3 done and reviewed at the user's checkpoint; stage 4 starts after the pre-stage-4 items below. Commits carry no AI attribution lines (user instruction).

## How to run checks
`scripts/dev.sh scripts/check.sh` (from a shell in the `docker` group) runs inside the pinned Python 3.12 dev container: hash-verified install, `ruff format --check`, `ruff check`, `mypy --strict`, `pytest` against the Compose Postgres. Start Postgres first: `docker compose up -d --wait postgres`.

## Stage 1 — Postgres schema + Compose + test harness
- [x] 1a Scaffolding: pinned Postgres 16.12/pgvector 0.8.1 image digest (loopback-only port 5433), pinned Python 3.12.16 dev image, config via env (`.env.example`), pooled DB with bounded lock/statement timeouts, per-session `pg_advisory_xact_lock`, checksummed forward-only migrations.
- [x] 1b Schema `0001_init.sql`: sessions/claims/runs, proposals/display receipts/approvals, operations/attempts, jobs/outbox/traces, memories (vector(768)), mock backend tables. Constraint backstops: one pending proposal per session, one approval and one operation per proposal, unique idempotency key, lifetime caps (3 dispatches/6 reconciliations/40 model calls/300 s), no double-booked provider slot, one booking per hold, inferred memory never active unconfirmed.
- [x] 1c Claims with generation fencing; tests for rollback, lock timeout → SessionBusy, lock serialization, unrelated sessions independent, concurrent claim (exactly one of 6 wins), expired-lease takeover fences old holder, stale release is a no-op.
- Gate result: **15 passed**; ruff/format/mypy strict clean.

## Stage 2 — exact mock tools ✅
- [x] 2a Tool contract models (extra=forbid inputs), errors with effect knowledge (none/unknown), internal ToolContext/Fence; deterministic seeded catalog (3–8 providers per service×area, injection review on each pair's cheapest), demo history; search/get_provider/list_bookings; ToolClient validation + per-call sanitized trace with latency. Tests: 28 passed.
- [x] 2b hold_slot (10 min, configurable quote drift, schedule/availability checks), book (70/15/15 with half of timeouts booked; forced-outcome FIFO; hold_expired), get_booking_status by hold or key (authoritative; configurable 503s); durable seeded RNG/IDs survive restart; replay records commit with effects; backend fencing (claim generation + reserved attempt + snapshot/key match). Tests: 56 passed.
- [x] 2c cancel (first call per key → durable transient 503 with no effect, same-key retry succeeds, configurable extra faults, provider-cancelled state reported as observed), reschedule (configured/unavailable/off-schedule/past → slot_conflict preserving the original; replay), `simulate_provider_cancellation` control. Tests: 68 passed. Gate E41 + backend parts of E08–E15 covered.

## Stage 3 — approval + journal + booking recovery ✅ (user checkpoint: awaiting review)
- [x] 3a Explicit state machine (validated, CAS-versioned, traced transitions); immutable snapshot revisions (one pending per session, identical re-propose is idempotent, blocked while an operation is unresolved); display receipts issued only when code renders the card; `yes` / `confirm <token>` bound to the receipt's still-pending revision; stale/superseded → specific material-change explanation, never transferred; expired hold → template; atomic approval + operation + stable key; duplicates return the same operation (also after restart and under 5-way concurrency). Tests: 35 new, 103 total.
- [x] 3b Executor: G7 exact-snapshot check (wrong tool/hold/key, extra or missing args → rejected, zero effect) then dispatch from the stored snapshot; durable attempt reservation with fence before each call; forced get_booking_status after every book (list_bookings for cancel/reschedule); same-key retry only after a verified "not done"; lifetime caps (3 dispatches/6 reconciliations) and run caps (tool calls, 300 s deadline) end with truthful outcomes (unknown → pending_verification, never claimed); cancel 503 → durable retry_wait + job row, same-key retry when due. Migration 0002. Real bugs BUG-001..003 logged in docs/BUGS.md with regression tests. Tests: 126 passed.
- [x] 3c RecoveryService (take over expired claim → fence old generation → fresh run → resume unresolved ops). Crash points: after reserve / after backend commit / after dispatch (timeout booked and lost) → status checked before any re-dispatch, exactly one booking. Crash after cancel retry reached backend → same-key resume. Repeat confirmation + execution after restart replays the same effect. Restart before dispatch never executes without a valid proposal. Live claim blocks a second worker; delayed worker (stalled before call / before recording) is fenced and cannot duplicate or overwrite; three unrelated sessions book concurrently. Concurrency tests stable over 5 repeated runs.
- Gate result (E05–E13, E23, E29, E32): **137 passed** (86 marked `invariant`); ruff, ruff format, mypy --strict clean.

## Stage 4 — G1–G11 and scripted model (in progress, user approved 2026-11-07)
- [x] 4a Deterministic parsing (G3): dates/times/budgets resolved against the injected clock in Asia/Kolkata; ambiguous ("next saturday"), past, invalid or unsupported → clarification; extracted spans must occur in the user text; service/area must be supported by user wording. Deterministic ranking (G4): budget/window/time/explicit-provider filters, then price ↑, rating ↓, provider ID, slot; exclusion counts for no-match; reschedule slots by closeness to the original time. Tests: 55 new, 195 total.
- [x] 4b Model layer (`errand_agent/llm/`): Ollama-shaped native-tool types; ScriptedModel recording every request; local-only Ollama adapter (loopback/service host, no cloud tags, `think: false`); per-turn Pydantic tool defs → schema + validator with real-value enums (G1/G2); `choose_tool` = 1 logical / ≤4 physical with conservative repair, one-call-only, offered-tool, schema and semantic checks (G10); `phrase` with grounding scan, one regeneration, then template (G6); delimited/escaped untrusted data blocks (G11); every physical request reserves run + operation counters and is traced with exact messages, tools, options, usage, latency and cost note. Per-operation model-call and active-time lifetime limits wired into caller and executor. Tests: 40 new, 235 total.
- [x] 4c Agent orchestrator (`workflow/agent.py`, `agent_tools.py`, `request.py`, migration 0003): one run per user message under the session claim; confirmations go to the gate before any model call; focused extract/act/phrase calls; single STATE_TOOLS allow-list checked when offering and dispatching (G1); per-turn enums of real values (G2); verbatim-span extraction check (G3); book, reschedule, cancel and same-as-last-time (via list_bookings + code-forced get_provider for area) flows; held quote over budget never offered; expired hold re-checks and needs a fresh card; reviews only in escaped data blocks, never in extraction (G11); numbered degraded menu with retry/choose/status/stop and direct `field: value` entry (G9). Scripted scenarios E01–E04, E16 (review + user injection), E25–E30 (all five hallucination attacks plus E30 menu), E33, E34, E39, E40, E08/E09 at conversation level; E38 counts 3/5/1. Real bugs BUG-004/005 logged. Tests: 21 new, 256 total.
- [x] 4d-a **Design v3 (user decision):** ModelMux (Claude Code or Codex, configurable) replaces local Ollama as the only real-model path. The user was shown the PDF §1 / v2 §3 conflict and chose replacement. DESIGN.md and DECISIONS §17 updated (interpretation and risk recorded; G8 seeding not enforceable through ModelMux; the 768 output cap not claimed). Ollama adapter removed; `llm/modelmux.py` adapter (tool_choice required, no parallel calls, ignored-params traced, key from env only, local-URL check) + `llm/factory.py`; README section. Tests: 260 pass.
- [x] 4d-b Smoke test (E42 revised), **Codex measured**: `scripts/smoke.sh codex` on `modelmux:codex:gpt-6.1-sol`, run twice (2026-11-07 18:31 and 18:37 UTC). Both runs: 3/3 PDF flows reached `completed`; 24 logical / 24 physical calls (17 act, 5 extract, 2 phrase); first-pass and after-retry valid rate 1.0; no rejected calls; no booking without approval. Per-call latency median 5.4 / 5.7 s, max 8.0 / 9.7 s; flows 50–58 s (plumber), 27–31 s (AC reschedule), 57–58 s (same electrician). ModelMux reported temperature, seed and max_tokens ignored. Reports: `artifacts/smoke/`. Real bug BUG-006 (wrong key variable in the runner) fixed with a regression test; 266 tests pass. **Claude not measured**: its provider is not logged in (`modelmux status`); run `scripts/smoke.sh claude` after `modelmux login claude`.

## Stage 5 — memory, embeddings, context (in progress, 2026-11-07)
- [x] 5a Memory store (`errand_agent/memory/`: `facts.py`, `embedder.py`, `store.py`; migration 0004). Typed fields only: preferred area, earliest acceptable time, preferred provider per service, and short user-worded notes; code builds the embedded text. Explicit facts from user statements; code-inferred, unconfirmed candidates from the last 3 completed, approved bookings (same provider per service; earliest start ≥ 09:00); separate confirm (stays "inferred"); correct revises value+vector+revision together, else `pending_embedding` (unused); forget clears value/vector/hash/words and leaves a tombstone so code never re-infers it; per-user numbers for inspect/correct/forget; per-user advisory lock in its own key space. Retrieval: SQL hard filters (user, active, confirmed, current embedding model, field, scope) in a materialized CTE before exact pgvector cosine, top 6, min 0.30, score rounded to 6 places, ties by number. DB checks enforce the lifecycle (active ⇒ embedded+confirmed; unconfirmed/pending/forgotten ⇒ no vector; forgotten ⇒ no value; one current fact per field+scope). Embedder output validated (shape, finite, non-zero) and unit-normalized; fixture embedder never invents vectors. Tests: 30 new (invariants written first), 296 total.
- [x] 5b Agent integration (`workflow/memory_commands.py`, agent memory section, templates). Memory commands are handled by code like confirmations and never call the model: show / "what do you remember about me", forget N, forget my area / earliest time / <service> technician, confirm memory N, correct memory N: value (area and time parsed by the request resolvers; a provider correction must name a real provider of the same service, checked with `get_provider`). Explicit "remember …" goes through `update_request` (`remember`, `remember_note`); the semantic check rejects it unless the user's message has explicit memory wording, and a note must be verbatim (E40 poisoning). Saved area / earliest time / preferred provider are applied as visible defaults recomputed every turn (never written into the request), so what the user says now wins and correct/forget apply from the next turn; a saved provider only moves first if it passes every filter. After a verified booking, code suggests inferred candidates, unused until confirmed. Existing 21 scenarios now run with memory enabled. Tests: 28 new (E19 ×3, E20 ×2, E40 ×2, commands, ownership, outage, parsing, favoured ranking), 324 total.
- [x] 5c Context builder (`llm/context.py`, caller and agent wiring, `memory/seed.py`). Every physical request is measured as UTF-8 bytes of the compact JSON of messages + tool schemas and is never sent above 4,096; prompts are built from required sections (refused with an explicit message if they can't fit in 4,096 − 600) plus optional sections in keep order: saved preferences (≤ 700 bytes, best-ranked first) then review excerpts (≤ 900 bytes, ≤ 300 chars each), so reviews drop first, then the lowest-ranked memories; retry diagnostics are byte-bounded inside the 600-byte reserve. Act calls carry relevant memories retrieved with a query built from the structured request only (never chat text), once per query per run; at most 4 embedding requests per run; an unavailable embedder means no memories, traced, not a failure. Every model.call trace logs the exact messages/tools, request bytes and the section plan (included memory numbers, dropped counts). Seed: 57 memories for the demo user in all five states + 6 for another user + 240 prior-conversation messages; every row that must never reach the model is marked CANARY and worded to be *more* relevant than legitimate notes. Mutation check: removing the user, embedding-model, scope or minimum-score filter from retrieval each fails the suite. Calibrated byte budgets recorded in DECISIONS §18 (the §3 token estimates didn't fit as bytes). Tests: 13 new (E21 ×3, E22 ×5, builder ×5), 337 total.
- [ ] 5d Real embedder: Ollama compose service on loopback, pinned image and `nomic-embed-text:v1.5` digest; `OllamaEmbedder`; integration pair-ranking check (E43).

## Pre-stage-4 requests (2026-11-07)
- [x] Git identity fixed and AI attribution removed from history by the user; no attribution in future commits.
- [x] Cross-check `docs/DESIGN_REVISION_v2.md` (saved from the user's paste) against DESIGN.md/DECISIONS.md: nothing from v2 is missing. Follow-ups recorded under Open items (job-queue dispatch, event ingestion, stale status lines, one-page check).
- [x] Write-ahead `tool.attempt_started` trace event before every tool call (same `call_id` as the completion `tool.call`); a crashed call still shows. 140 tests pass.
- [x] Open items recorded below.
- [x] README with setup and the PyPI mirror/hash-verification note.

## Open items
- [x] **Per-operation model-call and active-time lifetime limits (done in 4b).** `reserve_operation_model_call` / `check_operation_time` / `charge_operation_time` in `workflow/budget.py`; ModelCaller charges model time and calls; executor checks before each reservation and charges each attempt (crashed attempts up to 60 s). Tests: model-call cap survives new runs, active-time cap refuses the forced status check and stays pending.
  Original note: Schema has `operations.physical_model_calls` (CHECK ≤ 40) and `operations.active_ms` (CHECK ≤ 300000), but nothing charges them yet. Wire them up in stage 4 with the model adapter: reserve per physical model request, charge active time (model, tool and lock waits; not queued retry or user waiting), conservatively charge outstanding work after a crash, and test that a new run cannot reset them (E23).
- [ ] **Retry worker (stage 6).** `operation_retry` job rows are written on a cancel 503 (`retry_wait`, `next_retry_at`), but no worker consumes them; retries currently happen only via `OperationExecutor.resume`. Stage 6: `FOR UPDATE SKIP LOCKED` job loop, due scheduling, claim/generation fencing, crash recovery, original lifetime budgets (E14/E31/E32).

- [ ] **Route booking dispatch and reconciliation through the job queue (stage 6).** v2 §4 lists booking dispatch, retries, reconciliation and cancellation follow-ups as `SKIP LOCKED` jobs. Today the executor dispatches inline in its caller; only cancel retries write a job row. Stage 6 must make the job worker the caller of `OperationExecutor` for these.
- [ ] **Unique constraint on provider-event ingestion (stage 6).** v2 §4 requires one; the cancellation-event table doesn't exist yet.
- [ ] **Stale status lines.** DESIGN.md line 3 ("implementation awaits approval") and DECISIONS.md line 3 / §15 heading ("pending approval, no code yet") predate the build approval. Update them with the next design-doc edit.
- [ ] **One-page check of DESIGN.md** (v2 §8.1) after the real trace excerpt is added (stage 7).

- [x] **Stage 5 decisions (user, 2026-11-07; DECISIONS §18):** embeddings via local Ollama `nomic-embed-text`, embeddings only; context cap enforced as ≤ 4,096 UTF-8 bytes of app-assembled messages + tool schemas (gateway overhead traced, not counted).

## Environment notes
- Host has only Python 3.14; toolchain runs in Docker (`Dockerfile.dev`, runs as uid 1000).
- This network downloads from PyPI's CDN at ~9 KB/s (confirmed by the user's own terminal). User approved using the Tencent PyPI mirror **with hash verification**: locks are `requirements*.lock` with sha256 hashes, installs use `--require-hashes`, and `scripts/verify_lock_hashes.py` checked all 31 pins against pypi.org's official metadata (0 failures). The mirror URL lives only in local `.env`.
- This agent's shell lacks the docker group; it runs Docker via `wsl.exe --distribution Ubuntu --user laptop --exec ...`.

<details>
<summary>Design-phase history (superseded where it conflicts with the above)</summary>

# Progress

Current scope: **Design Revision v2 complete; awaiting user approval before any application code.** The assignment PDF remains authoritative; all eight contracts and every requirement are retained.

## Design Revision v2 — 2026-11-07
- [x] Re-read the PDF and v1 design/decisions; apply the user's overriding v2 instructions.
- [x] Update docs/DESIGN.md as a compact 513-word design note.
- [x] Update docs/DECISIONS.md with current sections 1–16, explicit supersession table, verbatim v1 decisions archive and prior compact note archive.
- [x] Specify G1–G11, native Qwen 3.5 tools, code-owned facts/templates, exact snapshot enforcement, focused calls and degraded menus.
- [x] Recount clarified booking: nine logical calls; ten with history; busiest ordinary run six with history. Retain run caps 10 logical/40 physical, 20 tools, 300 active seconds; lifetime operation caps retained.
- [x] Replace SQLite/singleton with PostgreSQL 16 + pgvector, session advisory transactions, durable claims/generation fencing, SKIP LOCKED jobs and stable-ID outbox. Tests use identical Postgres stack.
- [x] Select Ollama nomic-embed-text:v1.5 at 768 dimensions; pin/verify artifacts during setup. Retain fixture-vector offline evals, real local embedder integration, SQL hard filters, atomic fact/vector lifecycle and no prior-chat replay.
- [x] Add complete PDF requirements trace table, E01–E45 planned scenario/acceptance families including all five hallucination attacks, and risk-ordered eight-stage build plan with gates.
- [x] Check G1–G11 presence, all E01–E45 IDs, eight build stages, 4,096-token allocation arithmetic and verbatim v1 archive preservation.
- [x] Consult official Ollama/model, PostgreSQL and pgvector references for the proposed interfaces; links recorded in DECISIONS.md.
- [ ] Verify final one-page rendered note: browser launch is sandbox-blocked; installed groff lacks the PDF device. Note is 513 words, but pagination is not verified. Recheck after the required actual trace excerpt is added.
- [ ] Await explicit user approval; no application code, Compose files, database/model setup, application tests or live inference performed in this task.

## Next work, only after approval

Follow docs/DECISIONS.md §15: (1) Postgres/schema/Compose/harness, (2) mocks, (3) approval/journal/recovery, (4) guardrails/scripted tests and Qwen smoke, (5) memory/embed/context, (6) jobs/retries/watcher/outbox, (7) trace/export/eval evidence, (8) FastAPI/thin CLI and delivery. Update this file and HANDOFF.md after every task. No test success or model digest/size/latency is claimed before execution.

DESIGN.mdx and DESIGN.plan.html remain superseded historical visuals. ModelMux setup is separate infrastructure, not the assignment inference path. Earlier entries below are historical and do not override v2.

<details>
<summary>Historical progress and superseded checkpoints</summary>

# Progress

## Active revision v2 — documentation only

User supplied Design Revision v2; it overrides earlier choices. In progress: PostgreSQL 16 + pgvector, FastAPI/thin CLI, native Ollama Qwen 3.5 tool calls, G1–G11, local Ollama embeddings, durable jobs/outbox, full PDF traceability and eight-stage risk-ordered build plan. Preserve superseded decisions. Do not write application code. On completion stop for the user’s explicit approval.


Current scope: **Errand Agent design only.** The 7 November design review restores local Ollama inference as required by the user. ModelMux/Docker setup is separate infrastructure; it is not the assignment model path. Do not implement the Errand Agent yet.

## Design review — 2026-11-07
- [x] Re-read the PDF and current design; address all eleven review items in docs/DESIGN.md and docs/DECISIONS.md.
- [x] Record keys/attempts, counted call budget, token allocations/drop order, local inference/JSON retries, both approval forms, memory authorship, watcher architecture, locking, embeddings, and durable cancellation retries.
- [x] Specify planned tests; no application code or tests written.
- [ ] Regenerate the visual companion: docs/DESIGN.mdx and docs/DESIGN.plan.html are superseded by the reviewed Markdown design.
- [ ] During implementation, pin downloaded inference/embedding artifacts and tokenizer/runtime dependencies; validate real prompt counts, relevance and latency.

## ModelMux setup — 2026-11-07
- [x] Read tracking files and rerun baseline tests: no application tests collected (exit 5).
- [x] Inspect upstream ModelMux and clone it into ignored `.local/modelmux` (HEAD `5fcddd1054cf7dc361d4e4b36547fe17146cf4fd`).
- [x] Verify this is Ubuntu 26.04 on WSL2 with systemd; Docker is absent and sudo requires authentication.
- [x] Install Docker Engine 29.9.3 and Compose 5.7.0 from Docker's official apt repository via WSL root administration; daemon enabled/running and user added to docker group.
- [x] Install ModelMux CLI 0.3.1.
- [x] Verify Docker runtime with `hello-world`; document the setup and user's login commands in `docs/MODELMUX.md`.
- [ ] Prepare both provider containers and local configuration.
- [ ] Verify container startup/configuration and report readiness accurately. User will perform both provider logins; do not authenticate for them.
- [x] Update design sources and regenerate/validate the HTML for ModelMux (vplan check passes; final browser verification pending).
- [ ] Record operation commands, final status, and next steps.

## Visual design document — 2026-11-07
- [x] Re-read progress/handoff and rerun baseline tests: no tests collected (exit 5).
- [x] Verify workspace write access and read Visual Plan's skill and static-export instructions.
- [x] Install the Visual Plan renderer (`npm i -g vplan@latest`).
- [x] Create `docs/DESIGN.mdx` from the PDF and existing design note: architecture, phases, state, approval, recovery, memory, fixed contracts, proactive events, and evaluation coverage.
- [x] Pass `vplan check docs/DESIGN.mdx` and render `docs/DESIGN.plan.html` using vplan 0.24.0.
- [x] Inspect the HTML in offline headless Chromium: six diagrams rendered, zero page errors, zero external requests, no desktop/mobile overflow; desktop screenshot visually reviewed.
- [x] Prepare the HTML link for the user and update handoff.

## Phase 1 — Discovery and design
- [x] Create PROGRESS.md (it did not exist at the start).
- [x] Read all three pages of Orange_Engineering_Assignment.pdf using pypdf.
- [x] Check repository and ancestor instructions; no applicable AGENTS.md found.
- [x] Run baseline tests: no tests exist (pytest exit code 5).
- [x] Present the proposed design before any application code.
- [x] Save the one-page design in docs/DESIGN.md.
- [x] Record handoff and respect the user's design-only instruction.

## Model hosting discussion — 2026-11-07
- [x] Research free hosted Gemini access and current paid Google/OpenAI token prices.
- [x] Estimate model costs using 10,000 input and 2,000 billable output tokens per complete errand conversation; offline scripted evaluations incur no model API usage.
- [x] User selected ModelMux as the gateway and requested both Claude/Codex containers; user will handle provider login. No paid API call has been made.
- [x] Research Ollama hardware sizing: official Qwen3 Q4 model downloads are 2.5 GB (4B) and 5.2 GB (8B). Practical RAM/VRAM guidance is an estimate for one concurrent request with short context, not an official minimum or a benchmark. No model installed or selected.

Requirement interpretation to resolve: the PDF requires local operation without external service integrations and separately requires a real LLM. A hosted model depends on interpreting the integration restriction as applying to real errand providers/payments; preserve a local option if fully disconnected operation is expected.

## Phase 2 — Implementation (paused; requires user instruction to resume)
- [ ] Implement all eight fixed mock tool contracts and reproducible scenarios.
- [ ] Implement local Ollama adapter, scripted offline adapter, and explicit workflow according to the reviewed design.
- [ ] Implement application-enforced approval, durable idempotency, and recovery.
- [ ] Implement bounded execution, memory controls, context selection, and traces.
- [ ] Implement proactive cancellation detection and replacement proposals.

## Phase 3 — Validation and deliverables (not started)
- [ ] At least 20 automated simulated-user scenarios covering every PDF requirement.
- [ ] Generated evaluation table and requirement-to-test mapping.
- [ ] Raw timeout-after-success trace and annotated design-note excerpt.
- [ ] README and three real bugs with regression tests.
- [ ] Assistant transcript and 3–5 minute screen recording.
- [ ] Git repository delivery (current .git directory is empty and read-only).
- [ ] Only after all PDF requirements pass: propose five extra features with problem, effort, and tests; stop for selection.

Test status: Baseline pytest still collects no application tests (exit 5). Visual Plan validation passes; final offline browser checks pass at 1440px desktop and 390px mobile widths. No application implementation or passing PDF requirement tests exist yet.

</details>


</details>
