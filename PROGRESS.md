# Progress

**Build in progress (started 2026-11-07, user-approved).** Source of truth: `Orange_Engineering_Assignment.pdf` (no `ASSIGNMENT.md` exists; no `docs/DESIGN_REVISION_v2.md` exists — v2 is in docs/DESIGN.md + docs/DECISIONS.md §§1–16). Build plan: DECISIONS.md §15. **User checkpoint: stop after stage 3 (approval gate + booking recovery) and show test results.**

## How to run checks
`scripts/dev.sh scripts/check.sh` (from a shell in the `docker` group) runs inside the pinned Python 3.12 dev container: hash-verified install, `ruff format --check`, `ruff check`, `mypy --strict`, `pytest` against the Compose Postgres. Start Postgres first: `docker compose up -d --wait postgres`.

## Stage 1 — Postgres schema + Compose + test harness
- [x] 1a Scaffolding: pinned Postgres 16.12/pgvector 0.8.1 image digest (loopback-only port 5433), pinned Python 3.12.16 dev image, config via env (`.env.example`), pooled DB with bounded lock/statement timeouts, per-session `pg_advisory_xact_lock`, checksummed forward-only migrations.
- [x] 1b Schema `0001_init.sql`: sessions/claims/runs, proposals/display receipts/approvals, operations/attempts, jobs/outbox/traces, memories (vector(768)), mock backend tables. Constraint backstops: one pending proposal per session, one approval and one operation per proposal, unique idempotency key, lifetime caps (3 dispatches/6 reconciliations/40 model calls/300 s), no double-booked provider slot, one booking per hold, inferred memory never active unconfirmed.
- [x] 1c Claims with generation fencing; tests for rollback, lock timeout → SessionBusy, lock serialization, unrelated sessions independent, concurrent claim (exactly one of 6 wins), expired-lease takeover fences old holder, stale release is a no-op.
- Gate result: **15 passed**; ruff/format/mypy strict clean.

## Stage 2 — exact mock tools (in progress)
- [x] 2a Tool contract models (extra=forbid inputs), errors with effect knowledge (none/unknown), internal ToolContext/Fence; deterministic seeded catalog (3–8 providers per service×area, injection review on each pair's cheapest), demo history; search/get_provider/list_bookings; ToolClient validation + per-call sanitized trace with latency. Tests: 28 passed.
- [ ] 2b hold_slot (10 min, quote drift), book (70/15/15, half of timeouts booked, hold_expired), get_booking_status; durable RNG; replay records; backend fencing.
- [ ] 2c cancel (first call 503 per key, durable phase), reschedule (configured slot_conflict).

## Stage 3 — approval + journal + booking recovery (not started)

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
