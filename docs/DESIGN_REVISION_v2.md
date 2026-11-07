# Errand Agent — Design Revision v2

**For the coding agent.** This document records design decisions made after reviewing `docs/DESIGN.md` and `docs/DECISIONS.md`. Apply them to those two files. **Do not write application code in this step.**

## 0. Ground rules

- `ASSIGNMENT.md` (the PDF) is the source of truth. **No PDF requirement may be dropped, weakened or reinterpreted.** The 8 tool contracts (names, inputs, outputs, failure behaviours) are fixed.
- Where this document conflicts with the current `DESIGN.md` / `DECISIONS.md`, this document wins. Where it is silent, keep the existing decision.
- Keep `PROGRESS.md` and `docs/HANDOFF.md` updated after every task. Assume you can be cut off at any moment and another agent will continue from those files.

## 1. Core principle: the LLM handles language, code owns facts

The local model is small and will hallucinate. The design must not depend on the model being correct.

- **The model does:** understand user messages, extract request fields, choose the next action from a restricted set, and phrase non-consequential replies.
- **Code does:** IDs, prices, slots, dates, ranking, approval, execution, success/failure claims, and every message that carries consequential details.

Every guardrail below applies this principle.

## 2. Hallucination guardrails (mandatory)

| # | Guardrail | Rule |
|---|---|---|
| G1 | **Tools scoped by state** | Each workflow state exposes only the tools valid in that state. E.g. `collecting`: `update_request`, `ask_user`. `comparing`: `search_providers`, `get_provider`, `select_option`. Consequential tools are exposed only in states where an approved operation exists. A tool that isn't offered can't be called. |
| G2 | **Enum-constrained IDs** | Tool schemas are rebuilt every turn. `provider_id`, `slot`, `booking_id` and `hold_id` are enums of values that actually exist in the current state/tool results. Invented values fail schema validation. |
| G3 | **Code resolves dates** | The model outputs relative expressions ("this week", "mornings", "Saturday", "not before 10 a.m."). A deterministic parser resolves them against an injected clock. The model never computes dates. |
| G4 | **Code ranks providers** | A deterministic scoring function filters by budget, time window and confirmed preferences, then ranks. The model may explain the ranking but cannot change it. |
| G5 | **Code renders consequential output** | Comparison tables, approval cards, prices, booking/cancel/reschedule confirmations and error outcomes come from code templates. The model never writes a price, slot or booking status the user relies on. |
| G6 | **Grounding check on every reply** | Before display, scan model text for numbers, currency, dates, provider names/IDs and status words ("booked", "confirmed", "cancelled", "rescheduled"). Each must match current state. On mismatch: regenerate once, then fall back to a template. Log the violation in the trace. |
| G7 | **Approved snapshot enforcement** | The model may *propose* `book` / `cancel` / `reschedule` (the LLM drives tool calls, per the PDF). Code ignores the model's arguments and dispatches only the exact approved snapshot (action, provider, slot, price, hold/booking, idempotency key). If the proposal doesn't match the approved snapshot, reject it and don't execute. After a timeout, code always forces `get_booking_status`; the model cannot skip or replace it. |
| G8 | **Small focused calls** | Split model work into narrow calls (extract/update request → choose next action → phrase reply) rather than one open-ended agent prompt. Temperature 0, fixed seed. |
| G9 | **Degraded mode** | If the model fails repeatedly (after repair + 3 retries), the workflow falls back to a deterministic template flow ("Reply 1, 2 or 3") so every exit path still gives the user a clear outcome. |
| G10 | **Invalid output handling** | Unchanged from DECISIONS §4: conservative local repair → up to 3 model retries → visible failure. Applies to malformed tool calls and arguments. Trace every attempt. |
| G11 | **Untrusted text isolation** | Reviews and other free text go to the model only inside a clearly delimited data block, never in system/instruction position. They can never create memories, approvals or permissions, or alter the request. |

**Required hallucination eval scenarios** (in addition to the PDF list): the scripted model (a) invents a provider ID, (b) misquotes a price in its reply, (c) claims a booking succeeded before verification, (d) calls `book` while still comparing, (e) proposes `book` with arguments that differ from the approved snapshot. Each must assert the bad output was blocked and no side effect happened.

## 3. Model runtime

- **Ollama, local, with native tool calling** (the `tools` parameter / `tool_calls` response), not "JSON matching a schema" prompting.
- **Chat model: Qwen 3.5** (tools-capable). Confirm the exact Ollama tag and size during setup and pin the model digest. Run a smoke test on the 3 PDF example requests (valid tool-call rate, latency) before finalising the size.
- No cloud fallback at runtime. Evals run offline against the scripted/recorded model.

## 4. Persistence: PostgreSQL (replaces SQLite)

- **PostgreSQL 16 + pgvector**, run locally via `docker compose`. One database for: sessions/workflow state, proposals/approvals, operation journal and attempts, mock provider backend state, memories and vectors, job queue, outbox, traces.
- **Tests use the same Postgres** (a Docker container). Don't keep SQLite for tests; the two would drift.
- Replace the OS advisory lock + singleton worker with:
  - **Postgres advisory lock per session** (`pg_advisory_xact_lock(session_id)`), so two workers never act on the same conversation.
  - **Job queue table** consumed with `SELECT … FOR UPDATE SKIP LOCKED` for booking dispatch, retries (e.g. the cancel 503 retry with `next_retry_at`), reconciliation and cancellation follow-ups.
  - **Outbox table** for user notifications (unique key per event/user, at-least-once delivery with stable IDs).
- Keep the existing rules: short transactions, never hold a transaction across model or tool calls, revision compare-and-swap, unique constraints on proposal→operation and event ingestion, mock side effects plus their replay record committed together.
- Keep all PDF-required recovery guarantees: restart mid-booking, no duplicate bookings across repeated confirmations/retries/restarts, bounded execution with lifetime limits per operation.

## 5. Memory and embeddings

- **Embeddings via Ollama** (e.g. `nomic-embed-text` or `qwen3-embedding`; pick one, pin it, record its dimension). No sentence-transformers/PyTorch.
- **Vectors stored in pgvector** in the same `memories` row as the fact. Correct/forget updates or deletes fact and vector in one transaction. No separate vector database.
- Retrieval order (unchanged in spirit):
  1. **Hard filters in SQL first:** user, scope, current revision, not forgotten, confirmed (unconfirmed inferred facts excluded), applicable field. Excluded facts never reach ranking.
  2. **Query text built from the current structured request** (not raw chat, never review text).
  3. Cosine similarity, top-k above threshold, within the memory token budget, stable tie-break by memory ID.
  4. Log the exact memories and full context sent each turn.
- Filters provide safety; embeddings only provide relevance.
- **Offline evals** use checked-in fixture vectors for queries and memories; the real ranking/filter/budget code still runs. A separate integration test checks the real Ollama embedder ranks known pairs correctly.
- **Inferred memories** stay as decided (DECISIONS §7): code creates candidates from structured user choices, they stay unconfirmed until the user confirms, and the model can't promote them.
- The **long seeded prior conversation** exists to prove it never reaches the model context. State this explicitly in DESIGN.md. "Same electrician as last time" resolves via `list_bookings`, not chat history.

## 6. Architecture (semi-scalable, not overkill)

```
CLI chat client ──HTTP──► FastAPI service ──► Workflow engine (state machine + guardrails)
                                                 │            │
                                     Ollama (chat + embed)   ToolClient interface ──► in-process mock tools
                                                 │
                                         PostgreSQL + pgvector
                                 (state · approvals · operations · memory · jobs · outbox · traces)
                                                 ▲
                              Background workers (job queue: dispatch, retries, reconciliation,
                                                 cancellation watcher, notifications)
```

- **FastAPI service + thin CLI client.** The CLI shows chat and the live trace side by side for the demo.
- **Stateless workers:** all state in Postgres, so any worker can resume any session.
- **ToolClient interface:** mocks run in-process now; they can move behind HTTP later without agent changes.
- **Traces:** Postgres table plus a JSONL export (required deliverable: timeout-after-success trace).
- **Explicitly out of scope:** Redis, Kafka, Kubernetes, microservices, separate vector DB, cloud LLM.
- Single LLM agent plus deterministic background workers (unchanged from DECISIONS §8).

## 7. Decisions carried over unchanged

- §1 Stable operation key + separate attempt IDs.
- §2 Run limits (recount after G1/G7/G8 split, and report the new numbers).
- §3 Context budget 4,096 in / 768 out and the drop order (re-validate with the Qwen 3.5 tokenizer).
- §4 Repair → 3 retries → fail.
- §5 Expired-hold and changed-price messages + fresh approval.
- §6 Both `confirm <token>` and plain `yes`, bound to the latest displayed revision.
- §11 Durable cancel-503 retry state (now driven by the Postgres job queue).

## 8. What to produce now (no code)

1. Update `docs/DESIGN.md` (still max 1 page for the design note) and `docs/DECISIONS.md` with everything above. Mark superseded decisions as superseded, don't silently delete them.
2. Add a **requirements trace table** to `DECISIONS.md`: every PDF requirement → design section → planned test(s). Every row must be covered.
3. Add the **guardrail table** (G1–G11) and the hallucination eval scenarios to the eval plan.
4. Give me a **build plan** split into small tasks, ordered by risk:
   1. Postgres schema + docker compose + test harness
   2. Mock tools (exact contracts, seeded/configurable)
   3. Approval gate + operation journal + booking recovery
   4. Guardrails G1–G7 with scripted-model tests
   5. Memory + embeddings + context builder
   6. Job queue, cancel retry, cancellation watcher, outbox
   7. Traces + eval runner generating the eval table
   8. FastAPI + CLI (UI last)
5. Update `PROGRESS.md` and `docs/HANDOFF.md`, then **stop and wait for my approval** before writing any code.
