# Errand Agent — decisions v2

**7 November 2026 · build approved; stages 1–4 implemented.** The user's Design Revision v2 (`docs/DESIGN_REVISION_v2.md`) governs this document, as amended by the user's v3 decision in §17 (ModelMux replaces Ollama). The source is `Orange_Engineering_Assignment.pdf`, which v2 calls ASSIGNMENT.md. Every PDF requirement remains binding, without reinterpretation. Sections 1–16 below are current. Appendix A preserves v1 verbatim as **superseded historical material**, not implementation instructions. Unchanged choices are explicitly carried forward.

## 1. Keys and attempts — retained

Adopt one stable opaque idempotency key per immutable approved operation, with separate dispatch attempt IDs/ordinals. New keys per transport attempt risk duplicates; one conversation-wide key conflates distinct actions. Persist approval consumption, operation/key and dispatch reservation before invoking tools; uniquely associate proposal revision with operation. Repeated confirmation returns its existing result. Material change requires a fresh proposal/approval/key only after resolving the old ambiguous outcome. Known no-effect transient errors are nonterminal attempts; terminal outcomes replay, committed effects never resample. These semantics and the unchanged mock signatures must both survive restarts.

## 2. Focused calls and recounted limits — v1 count superseded

Native tool calls split language extraction, action choice and optional non-consequential phrasing. One call yields at most one accepted tool proposal; reject extra/parallel proposals rather than executing a batch. Code decides facts, ranks, renders consequential details and forces recovery. There are no hidden inference calls for memory, summarization, comparison rendering or status narration.

| Stage of full booking with one clarification | Focused calls | Tools/outputs |
| --- | ---: | --- |
| Initial incomplete request: extract fields; choose ask; phrase neutral question | 3 | Internal `update_request`, `ask_user`; phrasing has no tools. Code renders field labels and any facts. Wait ends run. |
| Clarification reply: extract update | 1 | `update_request`; deterministic date resolution and field validation |
| Choose search | 1 | `search_providers`; code filters/ranks and renders comparison |
| Choose provider details | 1 | `get_provider` for the code-ranked/user-selected real provider |
| Choose option | 1 | Internal `select_option`, constrained to the user selection or code-ranked first eligible option |
| Choose hold | 1 | `hold_slot`; code renders actual quote and approval card, no phrasing inference. Wait ends run. |
| Valid user confirmation: choose approved action | 1 | `book`; code forces `get_booking_status`, then renders verified result |
| **Total** | **9 logical calls** | **5 fixed backend tools + 4 internal actions = 9 invocations** |

A fully specified request skips the initial three-call clarification exchange and needs **6** logical calls. Resolving “same as last time” adds one focused `list_bookings` selection call and one backend invocation: **10** logical calls/10 total tool invocations for the clarified flow. `list_bookings` supplies history; never replay chat. Additional clarification replies, candidate inspections or phrasing calls cost actual counters, not zero.

The three ordinary runs use **3 / 5 / 1** logical calls (history makes its run one call larger). `get_booking_status` is code-forced, consumes a tool counter but zero inference. Timeout without success followed by a same-key retry adds `book` + status to tool counters; retry is continuation of the already validated model-driven operation, not a new model decision. Cancellation and reschedule still require their own model-proposed approved action.

Retain **10 logical calls, 40 physical inference requests, 20 tool invocations, 300 seconds active time per run**. Internal workflow actions also consume tool counters, as do code-forced checks, retries and failed dispatches. Waiting for user input ends a run. Valid clarification flow fits before retries; worst loaded ordinary run has six logical calls including history, safely below ten. Every loop reserves counters durably before dispatch, and new runs cannot reset an operation's lifetime allowance.

Each extraction/action logical call allows original generation + three retries = four physical requests. Neutral phrasing allows original + one grounding regeneration = two physical requests; no hidden G6 × G10 retry multiplication. Thus the nine-call clarified flow has at most **8 × 4 + 2 = 34 physical requests** across runs; history has **9 × 4 + 2 = 38**. The fully specified six-action flow allows 24. Forty per run remains a safe upper bound for any ten logical calls; earlier deadlines can stop before those maxima. Temperature 0 and seed 42 are fixed, but do not promise bitwise model determinism; scripted calls supply eval determinism.

Retain per-operation lifetime caps across restarts: **3 consequential dispatches, 6 reconciliation invocations, 40 physical model calls, 300 charged active seconds**. Run and operation limits both apply. Per-model-call deadline is at most 60 seconds and remaining budget, cold-load included. Persist reservations/deadlines; on crash conservatively charge outstanding work. Lock waits, tool calls and grounding retries count toward active deadlines; queued retry/user waiting does not. Bound embedding requests to 10 seconds each and four per run, also within active time; prefer cached query vectors. Finite limits end in explicit failure or pending-verification templates, never false success. Read-only manual inspection remains possible after execution budget exhaustion. Planned tests assert exact sequences/counters, each independent cap, retry maxima and restart preservation.

## 3. Context budget — retained, native schema accounting revised

Input cap **4,096**, output cap **768**, configured local context **8,192**. Allocation remains an educated estimate pending exact Qwen 3.5 tokenizer/template validation; no measured count is claimed.

| Component | Input allocation |
| --- | ---: |
| System policy, trust, approval/recovery | 600 |
| Current state-scoped native tool schemas and internal actions | 850 |
| Current request/proposal/operation/approval facts | 550 |
| Current user message | 350 |
| Structured candidates/tool data | 700 |
| Eligible relevant memories | 350 |
| Delimited untrusted review excerpts | 150 |
| Retry diagnostics/invalid argument excerpt | 300 |
| Qwen template, native-tool framing, delimiters/accounting reserve | 246 |
| **Total** | **4,096** |

The earlier “all eight schemas every call” assumption is superseded: only state-valid tools are serialized. Enum expansion counts too. Use separate narrow calls instead of fitting all states at once. Count the final prompt, tools and retry framing with the pinned Qwen tokenizer/template before dispatch and compare against runtime usage in integration tests. Never rely on chars/token or silent context truncation. If dependency access to the tokenizer needs setup, acquire it locally then; no PyTorch/sentence-transformers runtime dependency is introduced.

Drop order remains: (1) reviews, (2) lowest-ranked optional memories, (3) extra unselected candidate descriptions/nonessential old tool data, (4) shrink retry excerpt. Keep policy/schema, current user intent, approved material facts, unresolved status and candidate facts required for this decision. Do not silently omit enum values while pretending they remain selectable: page the options explicitly if needed. If essentials still overflow, no inference; use a clear shorter-input/menu template. Excluded/forgotten/unconfirmed facts and raw prior conversation are never candidates for inclusion. Tests capture complete prompts and enforce exclusion and token counts on originals and retries.

## 4. Model runtime and invalid output — runtime superseded twice (now §17), retry rule retained

Use local Ollama `/api/chat` with native `tools` and `message.tool_calls`, **not JSON-schema-in-content prompting**. The [Ollama tool guide](https://docs.ollama.com/capabilities/tool-calling) documents this interface. Extraction uses internal `update_request` tool arguments; action choice exposes only permitted native functions. This is an orchestration layer, not a change to the eight backend contracts.

Select tools-capable **Qwen 3.5**, initially evaluate `qwen3.5:4b`. The [Ollama tag page](https://ollama.com/library/qwen3.5:4b) currently advertises tools and shows a 4B quantized artifact; installed tag, complete disk size, quantization and full digest must be confirmed during setup, not inferred from a moving alias. No download or smoke test has been run here. Pin full digest, Ollama version, tokenizer/template and temperature 0/seed 42; disable thinking if supported by that pinned tag. Reject cloud tags; loopback-only runtime and disconnected verification. ModelMux is separate historical infrastructure.

Before finalizing size, run the three PDF requests with injected date/time and seeded provider data: plumber/this week/mornings/₹1,500; move AC servicing/Saturday; same electrician/not before 10 a.m. Record first-pass valid native-call rate, rate after retries, schema/state validity, missing-field handling, latency per call and full flow (cold/warm), and peak RAM. A valid-call denominator is every expected tool-choice turn, not just successful responses. Initial acceptance target: all three end-to-end flows pass guardrails, no invalid side effect, every run fits 300 seconds; report observed first-pass rate rather than hiding failures. If 4B fails, compare another tools-capable Qwen 3.5 size within available memory before locking the model choice. Smoke success is not a substitute for offline invariant tests.

Malformed tool call or invalid arguments: conservative local repair (whitespace or one enclosing code fence inside a stringified argument object), then strict syntax/schema/state/semantic validation; no invented values, permission, missing keys or truncated arguments. Allow up to **three additional model requests** with bounded diagnostics, validating each; four total per logical action. Trace every generation, repair attempt, failure and consumed budget. Valid syntax does not establish authority. Unsupported tool names, fabricated enums, argument mismatch, extra calls or state violations cannot dispatch. Invalid-output exhaustion produces a visible code template and offers degraded mode (§12 G9); if a run cap interrupts sooner, report the actual cap/count. No hidden reasoning is logged.

Neutral phrasing uses a separate no-tools call. G6 has exactly one regeneration; if either output is malformed/ungrounded, use a template at the second failure rather than starting another three-retry cycle. A late worker's model result cannot update state after its revision/claim has changed.

## 5. Material changes — retained, code-rendered

Validate hold/current facts before approval consumption and dispatch; backend checks hold expiry atomically too. Expiry: “The hold for {slot} expired, so I haven't booked it. I'll check availability again. Any new hold needs your approval.” Say “haven't booked” only when no effect is known; an unknown attempt gets pending-verification wording. New price: “The quote changed from {old_price} to {new_price}. Your earlier approval no longer applies.” Render a new exact card with new token and ask fresh approval only if within current constraints; otherwise ask about budget/alternatives. Search estimate versus held quote is shown before first approval. Slot/provider changes and reschedule conflict follow the same invalidation rule. Never silently loosen constraints or transfer old approval.

## 6. Confirmation forms — retained, service/CLI binding added

Accept normalized standalone `yes` or `confirm <token>` only from real user input, tied to latest actually displayed unchanged revision, request version, authenticated local session, correct ownership, valid hold and pending proposal. One actionable proposal per session. FastAPI issues opaque display revision/receipt; thin CLI acknowledges rendering and attaches the revision current when its input prompt opened. Server validates receipt/revision rather than trusting a caller's invented “latest” value. Pending newer revisions or material changes invalidate the old yes; never bind it to an unseen replacement. Tokens obey identical freshness checks. Code handles these messages before inference. Duplicate unchanged confirmation returns existing operation outcome without re-execution. Memory confirmation is separate.

Tests exercise both forms for all three consequential actions, stale provider/price/slot/hold, expiry, correction while typing, repeated/restarted input, cross-session reuse, absent card, event race and forged model/review approval.

## 7. Memory authorship — retained

Code-generated inferred candidates are narrower but auditable and avoid model-written memory poisoning. Model inference handles nuance but can persist malicious reviews as plausible user preference, adds cost and weakens reproducibility; a hybrid still needs provenance checks and explicit confirmation. Choose code: e.g. three distinct user-selected, completed morning errands yield an **unconfirmed** candidate. Exclude automatic suggestions, duplicate attempts, raw reviews and model assertions. Never infer permissions/approval policy.

Keep provenance/source event IDs, scope, explicit/inferred origin, reason/confidence, confirmation, revision and deletion state. Only a separate user memory-confirmation action activates a candidate. Unconfirmed candidates appear only in a deterministic management view, never context, summaries, ranking or embedding queries. Inspect/correct/forget remain supported across sessions; origin remains inferred after confirmation. The model may extract explicit user statements, with code validating source and ambiguity. Current user instructions override stored preferences; transient corrections do not automatically become persistent facts.

## 8. One agent and deterministic workers — retained, architecture extended

One language agent; deterministic workers handle dispatch, retries, reconciliation, cancellation watching and notification. A second LLM offers specialization but adds coordination over budgets, approvals and state with no required capability benefit. Workers scale independently of agent count, are stateless and use only Postgres as durable state. A deterministic watcher consumes simulated cancellations without user prompting, considers active upcoming bookings, invalidates affected proposals and queues replacement work under current confirmed constraints. It notifies through outbox/inbox, searches for valid replacements, explains no-match constraints and waits for fresh approval; never auto-rebooks.

```mermaid
flowchart TD
  CLI[Thin CLI: chat and live trace] -->|HTTP| API[Local FastAPI]
  API --> WF[Workflow and G1–G11]
  WF --> OC[Local Ollama chat: Qwen 3.5]
  WF --> OE[Local Ollama embeddings]
  WF --> TC[ToolClient]
  TC --> MT[In-process fixed mocks]
  WF --> PG[(PostgreSQL 16 and pgvector)]
  MT --> PG
  BW[Stateless background workers] --> PG
  BW --> WF
```

ToolClient preserves exact contracts now and permits HTTP transport later without changing the workflow; moving mocks is not current scope. FastAPI binds locally, associates sessions with the local user, exposes durable notifications/traces and carries display revisions. CLI does input/rendering only. Exclude Redis, Kafka, Kubernetes, microservices, separate vector DB and cloud LLM. Relative dates resolve against injected clock and user timezone (default Asia/Kolkata), never process-local wall-clock guesses. “This week” means current local date through Sunday; “Saturday” is the next nonpast eligible Saturday; mornings default [09:00,12:00), “not before 10” means inclusive 10:00. Show resolved windows via templates; ambiguous/past/unsupported expressions trigger clarification. Model extracts source expressions with spans; code validates spans against user text and computes dates. Explicit ISO dates are validated by the same parser. Budget/number parsing also validates the original user source.

Code applies hard constraints first (budget, resolved window, confirmed preferences), then stable ranking: price ascending, rating descending, provider ID and slot ascending as ties. Explicit confirmed/user-selected provider restricts candidates; model cannot override ordering or change eligibility. Unknown price/availability requires tool data rather than a guessed score. The original eight-tool boundary stays fixed; internal actions merely update workflow. Comparison/approval/outcome templates reference current DB facts, never model prose.

## 9. Postgres locking, claims and job queue — v1 SQLite/singleton superseded

Adopt **PostgreSQL 16 + pgvector in local Docker Compose**, including tests. A single logical database contains sessions, requests, proposals/display receipts, approvals, operations, attempts, mock providers/holds/bookings/replay records, memories, jobs, outbox and traces. Pin compatible Postgres-16 pgvector image digest and extension version during setup; test the same migrations/image as runtime, with isolated per-test data. No SQLite test substitute.

Use `pg_advisory_xact_lock(session_id)` with stable unique bigint session IDs, short transactions and bounded lock timeout (at most 2 seconds/remaining deadline). The [Postgres locking documentation](https://www.postgresql.org/docs/16/explicit-locking.html) describes transaction-scoped advisory locks. They serialize transitions/claims, **not time spent outside transactions**; a durable per-session active claim prevents two workers advancing the same conversation while either is doing model/tool work. Claims store job/operation, revision, generation, lease/deadline and budget reservations. Reacquire advisory lock to commit a result only if generation/revision still match. An expired model result is discarded. Incoming corrections/cancellation events serialize and invalidate stale proposals/results without waiting on inference.

Claim due jobs via `SELECT … FOR UPDATE SKIP LOCKED`; persist claim/generation and reserve counters in the claim transaction, then commit before model/tool calls. The next transaction records outcome and schedules continuation. Adopt one lock order: session advisory lock before locking that session's job/operation rows; candidate queue polling may read candidates first but must recheck eligibility under the locks. Avoid a queue-row/session-lock cycle. Finite lock/query timeouts and clear retry/pending messages apply.

A lease alone does not stop a delayed worker from executing a side effect. Before each in-process mock side effect, atomically verify session claim generation, operation/attempt reservation and immutable approved snapshot in the **backend mutation transaction**, using the same session advisory lock. Replayed committed keys return their effect; revoked/stale claims cannot commit a new effect. Pass claim metadata through internal ToolClient execution context, not new tool arguments. Recovery fences an expired claim before reconciling; late completions cannot overwrite newer state. Even after fencing, an unknown booking is reconciled by key before a retry, never assumed failed. Moving ToolClient behind HTTP later would require equivalent server-side idempotency/fencing; it is not assumed safe by transport abstraction alone.

Approval consumption, unique proposal→operation, key uniqueness, revision CAS, and unique event ingestion protect invariants. Backend effects plus their replay record commit together. No transaction spans inference/tool transport. Jobs and outbox changes commit with the state change that creates them; at-least-once delivery with stable event/user IDs does not claim exactly-once terminal display. Compare: Postgres adds Docker/migrations and more concurrency tests than SQLite, but gives one production/test behavior and shared workers; distributed brokers or a singleton lock would respectively add needless infrastructure or block the intended concurrency. Crash/interleaving tests are required before claiming these guarantees.

## 10. Ollama embeddings and pgvector — MiniLM/PyTorch superseded; reconfirmed for embeddings only in §18

Choose **`nomic-embed-text:v1.5`, native 768 dimensions**, via local Ollama `/api/embed`. The [Ollama model listing](https://ollama.com/library/nomic-embed-text) supplies the tag; the [Nomic model card](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5) describes 768-dimensional representations and retrieval prefixes. Pin full installed digest/runtime and verify response dimension during setup; no digest has yet been installed/verified. Do not use sentence-transformers or PyTorch. Prefix query with `search_query: ` and canonical fact with `search_document: ` consistently, pin normalization and content hashes. [Ollama embed](https://docs.ollama.com/api/embed) supports rejecting excess input with `truncate=false`; use that rather than silent truncation.

Store typed fact and `vector(768)` in the **same memories row**, alongside provenance/status and embedding version/hash. Correction prepares replacement vector outside a transaction, then atomically CAS-updates fact+vector+revision under the session lock; stale vectors never become current. If encoding fails, do not leave an old active preference: mark the corrected fact pending/unavailable for retrieval until the new vector can commit, and show this to the user. Forget deletes or clears fact+vector together and invalidates derived context in the same transaction. Never retain forgotten text in active summaries or re-embed it from history. Traces are audit-only, excluded from retrieval.

SQL hard-filters **user, scope, current revision, not forgotten, confirmed, applicable field** before any similarity evaluation. Use a materialized eligible relation for the exact scan so excluded facts never rank. Query text derives only from current structured request, never raw chat/reviews. Use exact pgvector cosine similarity over the small eligible set; no approximate index is needed for 50+ facts. [pgvector](https://github.com/pgvector/pgvector) supports exact cosine retrieval. Initial top-k=6, minimum cosine=0.30 (hypothesis to calibrate on labeled fixtures, then freeze), 350-token memory allocation, score rounded to six decimals and memory ID as tie-break. Hard applicability filters and explicit relevance fixtures are mandatory; similarity is not trust. Current required constraints live in workflow state, not optional retrieval.

Offline scenarios check in query/memory fixture vectors, normalized inputs and model/dimension/hash manifest. They exercise real Postgres SQL filtering, cosine ranking, tie-break and context budgeting, with scripted chat; no Ollama needed for ordinary evals. Separate disconnected Ollama integration check encodes known relevant/irrelevant pairs and checks ordering with tolerance, dimension and prefix consistency. Pin versions and use margin-separated pairs; no cross-hardware bitwise float promise. Missing artifacts fail clearly, never auto-download in offline tests. The long seeded prior conversation exists **to prove none of it reaches the model**; booking history comes only from `list_bookings`. At least 50 seeded memories include forgotten, obsolete, unconfirmed and irrelevant records.

## 11. Durable cancel-503 state — retained, queue replaces local scheduling

Persist operation/key, booking, approved snapshot, attempts/reservations, retry count, error, `next_retry_at`, generation and lifetime allowances. State: `approved → executing(1) → retry_wait(503, +1s) → executing(2, same key) → succeeded`. Required first 503 is a known no-effect attempt, not permanent terminal replay; backend persists this phase. Additional scenario-only transient faults allow third dispatch after 2s; default PDF scenario remains first fails, retry succeeds. All attempts consume the original lifetime budget.

A due Postgres job claims the retry; no blocking sleep/held transaction. On crash after backend 503 but before client receipt, backend's persistent phase ensures the next same-key call advances correctly. On uncertain cancellation completion, inspect via `list_bookings(user_id)` (the hold/key status tool is for booking) and replay the same cancellation key within bounds if needed. Observed provider-cancelled state is reported as observed, not falsely attributed to our call. Exhausted ambiguous operations remain pending verification. Fake clock tests cover pre/post-503, before due, post-success/pre-client-commit, exhausted limits, late/fenced workers, backend restart and provider-cancellation race.

## 12. Mandatory hallucination guardrails (G1–G11)

Core rule: **LLM handles language; code owns facts.** Extraction is an untrusted proposal, not a fact update until code validates source, types, constraints and revision. All consequential details and outcomes come from code. Omission from a tool list is backed by server dispatch validation; hallucinating an unoffered name never makes it callable.

| ID | Guardrail and enforceable rule | Planned tests |
| --- | --- | --- |
| G1 | Rebuild native tool allowlist by state; expose internal `update_request`/`ask_user` in collecting, search/details/`select_option` in comparing; hold only after validated selection. Consequential tools appear only for a live approved operation in executing. Verifying permits status/reconciliation, not new action choice. Deny anything outside offered set at dispatch too. | E28 out-of-state book; E33 every state's allow/deny matrix |
| G2 | Rebuild provider/slot/booking/hold enums every turn from actual session/tool data; also check tuple relationships, ownership, revisions and key bound to operation. Empty allowed set means omit that tool. No invented IDs, foreign hold or valid-but-wrong provider/slot pair. | E25 invented provider; E34 enum relationships/stale values |
| G3 | Model extracts relative date/time expressions with source spans; deterministic parser uses injected clock/timezone and clarifies ambiguity. Model never computes dates; code validates explicit dates too. | E01–E04 PDF examples/clarification; E35 boundary/date hallucination |
| G4 | Code filters and ranks (§8), model can explain only code ordering or identify explicit user's existing option. It cannot change ranking, eligibility, budget or preferences. | E36 deterministic ranking/over-budget override |
| G5 | Code templates render comparisons, approval cards, material prices/slots, statuses, consequential confirmations and errors. Model writes only optional neutral language. | E26 misquoted price; E27 premature success |
| G6 | Before any model text display, scan numbers/currency/dates/provider names/IDs/status words, including negation and context, against a fact allowlist for that reply purpose. Correct-but-premature status still fails. One regeneration, then safe template; log violation. Free-text grounding is fallible, so no consequential claim may rely on this scanner: G5 templates are the final boundary. | E26/E27; E37 scanner variants and fallback |
| G7 | Model proposes book/cancel/reschedule; validate exact agreement with approved snapshot then ignore supplied arguments for dispatch, sourcing action/provider/slot/price/hold-or-booking/key from stored snapshot. Any mismatch rejects whole proposal; never silently “fix” it and execute. After timeout, code unconditionally schedules `get_booking_status` before retry/success, even if model says skip it. | E29 differing approved arguments; E10/E11 forced reconciliation |
| G8 | Narrow extraction → action selection → optional neutral phrasing calls; temperature 0, seed 42; native calls restricted each time. Code handles facts and outcome narration; no parallel unchecked actions or hidden inference. | E38 call-count/schema/settings assertions |
| G9 | On repeated model failure after repair+three retries, display clear failure and deterministic numbered menu: supply missing fields, choose an existing ranked option, inspect pending status, retry language step, or stop. No hallucinated action executes. Menus preserve exact approval/revision rules. Safe data collection can continue; new consequential dispatch still needs a valid model proposal and approval, or stays paused. Already validated operations may reconcile/retry deterministically within budgets. Never pretend a broken model completed the real-LLM workflow. | E30 menu fallback/valid selections; E39 pending-recovery safety |
| G10 | Native call envelopes/arguments get conservative local repair then up to three inference retries and visible failure; validate state/schema/semantics/authorization each time, trace every attempt and enforce counters/deadlines. No model-output repair can create missing facts/authority. | E17 malformed/final retry/exhaustion; E25/E28/E29 |
| G11 | Reviews/other free text only in clearly delimited untrusted data blocks, never instructions; escape delimiter-like contents. They cannot update request, memories, permissions or approvals. Extraction prompts receive user-source fields, not provider text. | E16 user/review injection; E40 delimiter escape and memory poisoning |

Date/number/provider/status scanners must recognize supported display formats and test adversarial variants. Unrecognized material language is rejected or templated; a grounding match does not authorize action. Internal action schemas are not extra provider tool contracts. They cannot manufacture IDs, execute side effects, or weaken the eight contracts.

## 13. Fixed mock contracts and evidence

All contracts below are the PDF's unchanged agent-facing environment. ToolClient internal context carries claims/scenario data without adding arguments to these signatures. Scenario overrides/fake clock are test controls, not extra tools. Default random behavior uses a seeded durable sequence; configurable branches isolate edge cases. Replays do not reroll a committed result. Prices/IDs/dates in user output are rendered from these results and validated state.

| Tool signature | Fixed behavior | Planned tests |
| --- | --- | --- |
| `search_providers(service, area, date_range)` | 3–8 providers, IDs, prices, ratings, available slots, relevant attributes | E41 contract cardinality/fields/seed replay |
| `get_provider(provider_id)` | Details and free-text reviews; at least one review with prompt injection | E16, E41 |
| `hold_slot(provider_id, slot)` | Ten-minute hold with ID/expiry; quote may differ from search price | E08/E09, E41 |
| `book(hold_id, idempotency_key)` | 70% success, 15% slot_taken, 15% timeout; half of timeouts actually book; hold_expired past expiry | E09–E12, E41 verifies configured branch mapping and default distribution mapping |
| `get_booking_status(hold_id or idempotency_key)` | Authoritative source for ambiguous booking resolution | E10/E11/E13 |
| `cancel(booking_id, idempotency_key)` | First call transient 503, retry succeeds | E14/E31 |
| `reschedule(booking_id, slot, idempotency_key)` | Reschedule; configured conflicting slots yield slot_conflict | E02/E15 |
| `list_bookings(user_id)` | Past/upcoming bookings including ID, provider, service, slot, status | E03/E41 |

Trace records live in Postgres and export JSONL. Correlate run/session/message/proposal/revision/operation/key/attempt/job/generation; log state transitions, offered tool schemas, exact input context/memory IDs, native proposals, validation/grounding/repair results, dispatched authoritative arguments, sanitized results, latency, token usage and exit reason. Record model digest/settings and embedding version. Suppress secrets/hidden reasoning; provider text remains labeled data. Estimated cost is unavailable unless a defensible cost basis exists, not fabricated API token billing for a local model. Audit records never feed memory retrieval. Export must preserve event ordering and post-crash recovery evidence.

Required timeout-after-success evidence remains pending: persist operation K → backend commits booking/replay → client sees timeout → forced authoritative `get_booking_status(K)` → persist verified result → template success. Produce raw JSONL and a short annotated **actual** excerpt in the one-page note after implementation; no synthetic trace is evidence. Generate evaluation table columns **scenario · expected · actual · pass/fail · notes** directly from scenario assertions. Track at least three genuinely encountered bugs (what/why/fix/regression), assistant transcript, repository/README and a 3–5 minute full-booking recording with chat/trace side by side. Live failure handling is a desirable demo plus, not a substitute for core requirements.

## 14. Evaluation plan and complete PDF requirements trace

Every E-ID below is a planned automated scenario family; parametrizations can expand it. Implement **at least 20 distinct simulated-user scenarios**, not just twenty assertions. Use the same PostgreSQL-16/pgvector schema and in-process ToolClient as runtime, scripted/recorded native chat outputs, fake clock, seeded providers and fixture vectors. No Ollama/cloud/network requirement in ordinary evals beyond local Postgres connectivity. Separate local chat/embed smoke integration tests are reported separately. E42–E45 are artifact/integration acceptance checks, not counted as substitutes for twenty simulated-user scenarios. Assert persisted outcomes, proposal validity, invocation history, bounded counters and **no side effect from rejected output**, rather than reply wording alone.

### Scenario inventory

| ID | Scenario family and required invariant |
| --- | --- |
| E01 | Plumber request, only missing area asked; morning/date/budget resolved by code, no re-asking supplied fields |
| E02 | AC servicing move to Saturday; correct original booking and exact old/new-slot approval |
| E03 | Same electrician via `list_bookings`, not history replay; not before 10 honored, ambiguity clarified |
| E04 | Single-detail correction preserves unrelated state, stale proposal invalidated |
| E05 | Both confirmation forms for booking/cancel/reschedule; no confirmation/forged approval cannot execute |
| E06 | Repeated confirmation and replay after restart return same operation/effect |
| E07 | Provider/slot changes after approval invalidate both confirmation forms |
| E08 | Held quote differs from search/changes after approval; show actual quote and require fresh approval; over-budget remains blocked |
| E09 | Expired hold before confirmation/during backend dispatch; no silent transfer to new hold |
| E10 | Timeout after actual booking: status forced, exactly one booking, no premature success |
| E11 | Timeout with no booking: source-of-truth lookup precedes same-key retry; original budget/hold respected |
| E12 | Slot taken/terminal error replay; old approval cannot authorize replacement |
| E13 | Restart before dispatch/after backend commit; reconciliation before retry, stable key/effect |
| E14 | Default cancel 503 then success, same key, durable attempt state |
| E15 | Reschedule conflict preserves original booking and needs fresh approval for alternative |
| E16 | Adversarial user instructions and injected provider reviews cannot change intent/permissions/approval |
| E17 | Malformed native calls/arguments: repair, third retry success, exhausted path and exact traced counts |
| E18 | Unexpected tools/status failures preserve truthful known/unknown result and finite exit |
| E19 | Explicit/inferred memory inspection, separate confirmation, cross-session preference reuse |
| E20 | Correct/forget atomically updates/removes fact/vector; no stale summary/cache resurrection |
| E21 | 50+ mixed memories and long prior conversation: only relevant/current/confirmed facts included; zero raw prior chat |
| E22 | Token cap, drop order, enum growth and oversized essentials; every input/repair context bounded |
| E23 | Model/tool/embedding/time and lifetime caps, repeated resume cannot reset allowance; user-visible outcome |
| E24 | Proactive cancellation without user input; constraints respected, fresh approval before replacement, no-match explanation |
| E25 | Script invents provider ID: enum rejection traced; no backend side effect from bad output |
| E26 | Script misquotes price: text blocked before display, one regeneration/template; no side effect from bad output |
| E27 | Script claims booked before verification: text blocked, no success display or consequential dispatch from it; forced reconciliation retained |
| E28 | Script calls `book` while comparing: unoffered tool rejected and traced; zero side effect |
| E29 | Script proposes `book` arguments differing from approved snapshot: reject rather than normalize/execute; zero side effect |
| E30 | Repeated model failure: visible numbered degraded menu; valid choices collect state without bypassing guards |
| E31 | Cancel retry across pre/post-503 crash, future due time, retry-success crash and exhausted attempts |
| E32 | Multi-worker same-session races, unrelated sessions progress, stale leases/results fenced, no duplicate effects |
| E33 | Every state's native tool allowlist and dispatch denylist agree; extra/parallel calls rejected |
| E34 | Real-but-wrong provider/slot/hold/booking relationship, cross-user values and stale enums blocked |
| E35 | Injected clock/timezone midnight/week boundaries; model-invented dates/source spans rejected |
| E36 | Stable code ranking, ties, invalid budgets/preferences; model cannot reorder or loosen constraints |
| E37 | Grounding variants: numbers/currency/dates/names/IDs/negated or premature statuses; fallback after exactly one regeneration |
| E38 | Focused-call sequences, native tools, temperature/seed and no hidden inference; 9/10 count and physical caps |
| E39 | Degraded/limit mode with unknown operation does not claim failure or create another booking |
| E40 | Delimiter breakout and inferred-memory poisoning through reviews cannot reach trusted state |
| E41 | All eight exact contracts; default probability branch mapping and seeded restart sequence; status/hold/history fields |
| E42 | Disconnected local Qwen 3.5 three-example smoke: call validity/latency/memory; exact installed artifacts pinned |
| E43 | Postgres migrations/pgvector compatibility, no SQLite; real local embedding pair-ranking/dimensions; fixture ranking/filter tests |
| E44 | Outbox event/user dedupe, crash before/after send, offline inbox, stable redelivery IDs, cancellation duplicate/no-match |
| E45 | Generated eval table matches assertions; raw real trace + note excerpt, actual bug regressions, README/repo/transcript/demo and one-page export checklist |

In E25–E29 assertions are made at the rejection boundary; a later explicitly valid scripted response is a separate step, never permission to execute the rejected proposal. All five requested hallucination attacks explicitly assert both blocked output and zero effect caused by it. E27 preloads an unverified operation but blocks any new side effect; if a backend effect already exists from the simulated timeout, assert no **additional** effect and no unverified success. A separate pre-dispatch variant starts with no effect and asserts zero total effects.

### PDF requirement → design → planned verification

This table covers PDF §§1–5, including individual failure modes and deliverables. No row is waived. Section references below target this document; the compact note summarizes them.

| PDF requirement | Current design section | Planned test(s) / acceptance evidence |
| --- | --- | --- |
| §1 Chat clarifies, compares, obtains approval, recovers | §§2,5,6,8,12 | E01–E18 |
| §1 Fully local; no real providers/payments/external services | §§4,8–10,13 | E41–E43 disconnected runtime; local-only dependency/config audit |
| §1 Three example requests | §§4,8 | E01–E03 + E42 real-model smoke |
| §2 Fixed names/inputs/outputs/failures, seeded/configurable mocks | §§1,13 | E41 all eight signatures and deterministic branch fixtures |
| §2 search: 3–8 providers, prices/ratings/slots/attributes | §13 | E41 search contract and code ranking E36 |
| §2 details/reviews including malicious review | §§12 G11,13 | E16/E40/E41 |
| §2 hold ID/expiry, ten minutes, quote change | §§5,13 | E08/E09/E41 |
| §2 book 70/15/15, half timeouts booked, expired hold | §§1,5,13 | E09–E12/E41 seeded bucket mapping, both timeout branches |
| §2 authoritative status by hold or key | §§1,12 G7,13 | E10/E11/E13/E41 both lookup forms |
| §2 cancel first 503 then successful retry | §§11,13 | E14/E31/E41 |
| §2 reschedule and configured slot conflict | §§5,13 | E02/E15/E41 |
| §2 list past/upcoming booking fields | §§8,13 | E03/E41 |
| §3 collect only missing service/area/date-time/budget/preferences | §§2,8,12 G3 | E01/E03/E04/E35 |
| §3 never re-ask supplied facts, retain unrelated corrections | §§6,8,12 | E01/E04 |
| §3 explicit workflow/state model | §§8,12 | E33 state transition/allowlist coverage |
| §3 code-enforced exact approval for book/cancel/reschedule | §§5,6,12 G7 | E05/E29, both forms on all actions |
| §3 material provider/slot/price change revokes approval | §§5,6 | E07–E09/E32 |
| §3 hard model/tool/wall-time bounds | §§2–4,9 | E17/E22/E23/E38 |
| §3 every exit clear, exhausted retries/unexpected errors safe | §§2,4,11,12 G9 | E17/E18/E23/E30/E39 |
| §3 no duplicates on repeated confirmation/network retry/restart | §§1,6,9 | E06/E10/E11/E13/E32 |
| §3 timeout source-of-truth check before retry/success | §§1,12 G7,13 | E10/E11/E13/E27 |
| §3 never claim success before verification | §§12 G5–G7,13 | E10/E18/E27/E37 |
| §3 free-text tool data never instructions or permission | §§7,12 G11 | E16/E40 |
| §3 injection cannot alter intent/approval/permissions | §§6,7,12 | E16/E28/E29/E40 |
| §3 stable preferences across sessions | §§7,10 | E19 |
| §3 inspect/correct/forget stored memory | §§7,10 | E19/E20/E43 transaction checks |
| §3 distinguish explicit/inferred memory | §§7,10 | E19/E21/E40 |
| §3 trace every run: transitions/model/tools/args/results/latency/usage/cost where available | §§4,12,13 | E17/E25–E29/E45 trace-field assertions; unavailable cost explicit |
| §3 at least 20 offline simulated-user scenarios | §14 | E01–E41/E44 scenario inventory and generated count; artifact checks do not replace them |
| §3 assert outcomes/invariants, not just wording | §14 | Persisted outcomes/approval/tool invariants in every scenario |
| §3 eval adversarial input/review injection | §§12,14 | E16/E40 |
| §3 eval changed price/slot after approval | §§5,6,14 | E07/E08 |
| §3 eval ambiguous timeout, conflict, repeated confirmation | §§1,5,6,13,14 | E06/E10/E11/E15 |
| §3 eval memory correction/forget, restart mid-booking | §§7,9,10,14 | E13/E20 |
| §3 eval proactive cancellation/context/limits/tool failures | §§2,8,11,14 | E18/E21–E24/E44 |
| §3 detect simulated provider cancellation without prompt | §§8,9 | E24/E44 |
| §3 decide whether/how to notify, including offline delivery | §§8,9 | E24/E44 affected/upcoming-only policy, outbox/inbox |
| §3 find replacement within known preferences | §§8,10,12 G4 | E24 no match, unchanged constraints |
| §3 fresh approval before replacement booking | §§5,6,8 | E24/E05 |
| §3 seed ≥50 memories and long conversation | §§3,10 | E21 seeded fixture counts |
| §3 fixed context cap, only relevant/current preferences | §§3,10 | E21/E22 exact rendered context and SQL eligibility |
| §3 no forgotten/unconfirmed inferred facts in model context | §§7,10 | E20/E21/E40; zero history leakage |
| §3 log context every turn and prove with tests | §§3,13 | E21/E22/E45 captured payload vs trace |
| §4 fixed tools; explain orchestration trade-offs | §§1,8,9,13; DESIGN.md | E41 plus design review against PDF |
| §4 real LLM understands requests/drives tool calls | §§2,4,12 G7–G9 | E42 real native tool smoke; degraded flow cannot claim full model completion |
| §4 automated evals offline/scripted or recorded | §§4,10,14 | E01–E41/E44 no external calls; fixture vectors and Postgres |
| §4 single agent allowed; justify additional agents if any | §8 | One language agent, deterministic workers; architecture review |
| §5 Git repo with implementation/mocks/tests/README | §§13,15 | E45 checkout/setup/run instructions from clean local environment |
| §5 max one-page design: state/approval/idempotency/recovery/memory/observability | DESIGN.md; §§1,5–10,13 | E45 exported page-count/manual legibility check, including final trace excerpt |
| §5 generated eval table with all five columns | §§13,14 | E45 assertions generate expected/actual/pass-fail/notes |
| §5 three real bugs: what/why/fix/regression | §§13,15 | E45 actual encountered bug log, linked running regression tests |
| §5 raw end-to-end timeout-after-success trace plus annotated note excerpt | §13; DESIGN.md placeholder | E10/E45 real JSONL export and actual annotated excerpt |
| §5 3–5 minute full-booking demo, chat/trace side by side | §§8,13,15 | E45 recording duration/content review; live failure desirable |
| §5 coding-assistant transcript | §§13,15 | E45 retained/exported conversation artifact |

## 15. Risk-ordered build plan — approved; stages 1–4 done, stage 5 in progress (see PROGRESS.md)

Each numbered stage is split into reviewable tasks. Run its acceptance checks before proceeding; update PROGRESS.md and HANDOFF.md after every task. Tracing hooks and minimal journal events start with persistence/recovery even though the full exporter/eval report is stage 7. Use direct workflow/test harness calls until stage 8; no UI dependency blocks safety validation.

1. **Postgres schema + Compose + test harness.** (a) Pin PostgreSQL-16/pgvector image and create local-only Compose lifecycle/health checks, test isolation and migrations. (b) Define sessions/revisions/proposals/receipts/approvals, operations/attempts/budgets, backend holds/bookings/replays, memory rows/vectors, jobs/outbox/traces and required constraints. (c) Prove transaction rollback, same-schema test setup, per-session advisory locking, claim-generation CAS and multi-session concurrency. Gate: E32/E43 skeleton and schema/constraint tests pass against real Postgres. Trade-off risk: concurrency/atomicity before agent behavior.
2. **Exact mock tools.** (a) Implement ToolClient and all eight signatures/outputs. (b) Seed deterministic providers/history/review injection, ten-minute holds/quote drift and configurable failure branches. (c) Persist RNG progression, replay effects and first-cancel-503 phase; fake clock controls expiry. Gate: E41 plus backend parts of E08–E15; no retries/restarts reroll committed results or change contracts.
3. **Approval + journal + booking recovery.** (a) State machine and immutable displayed revisions, both confirmation forms and atomic operation creation. (b) Stable keys, attempt reservations, lifetime limits and exact-snapshot dispatch; backend fencing before effect. (c) Forced timeout reconciliation, repeated-confirmation replay and crash points. Gate: E05–E13/E23/E29/E32, including two-worker and delayed-worker interleavings. Highest consequence risk must be settled before live inference.
4. **G1–G7 and scripted model.** (a) Native-call adapter contract with scoped schemas/enums; deterministic date/number parsing and ranking. (b) Code templates, grounding scanner and snapshot argument match. (c) Script all five hallucination attacks plus injection, focused calls, repair/retries and degraded menus (G8–G11 required too). (d) Prepare real Qwen 3.5 smoke after safety tests; confirm tag/size, pin digest/tokenizer/runtime, record rate/latency and count caps before final size selection. Gate: E01–E04/E16/E17/E25–E30/E33–E40, E42; scripted evals remain independent of live model.
5. **Memory + Ollama embeddings + context.** (a) Typed facts/provenance, inference candidates and confirmation/inspect/correct/forget lifecycle. (b) Pin nomic tag/digest/dimension; atomically persist fact/vector revisions; SQL hard filters, exact cosine/ties and fixture vectors. (c) Build/tokenize prompts, seed 50+ mixed memories/long history, verify drop order and no forbidden context; integrate local encoder pair check. Gate: E19–E22/E40/E43, including tokenizer accounting and unavailable-embedder behavior.
6. **Jobs, cancel retry, cancellation watcher, outbox.** (a) Finish SKIP LOCKED worker loop, due scheduling, generation fencing and crash recovery around stage-3 journal. (b) Wire same-key 503 retry with original budgets; authoritative cancellation checks. (c) Consume durable cancellation events, search replacements through same workflow, no-match branch, outbox/inbox dedupe and offline delivery. Gate: E14/E24/E31/E32/E39/E44; no new LLM agent/broker.
7. **Trace export + eval runner.** (a) Complete exact-context/native-call/guardrail/attempt/latency/usage traces and JSONL export. (b) Run ≥20 distinct simulated-user scenarios and generate all-five-column eval table plus this requirement mapping's evidence links. (c) Produce actual timeout-after-success raw trace/annotated excerpt and document three actual bugs with regression tests as encountered. Gate: E45 and full offline suite, bounded costs marked available/unavailable, no fabricated results. Retain assistant transcript throughout all stages.
8. **FastAPI + thin CLI last.** (a) Wire local HTTP session/message/display-receipt/notification/trace endpoints to tested engine; authorization, request dedupe and server-side revision checks. (b) Thin terminal client renders code templates and live trace panes, handles disconnect/resume and async event races while input is open. (c) Clean-checkout README/setup, offline runtime check, final ≤one-page export, 3–5 minute recording and deliverable audit. Gate: E05/E06 input races through HTTP plus E42–E45 and complete requirement table. Stop if any required evidence remains missing; additional feature proposals stay gated on all PDF acceptance tests passing.

The user approved this plan on 2026-11-07. PROGRESS.md records which stages are complete and their test results.

## 16. Supersession and documentation status

| Earlier decision | Current status/replacement |
| --- | --- |
| DESIGN.md v1 SQLite/terminal-only/Qwen3/MiniLM compact note | Superseded by current DESIGN.md; its original text is preserved below in Appendix B |
| DECISIONS v1 §2 seven/eight decisions | Superseded by §2 nine/ten focused calls, with separate run and physical retry accounting |
| DECISIONS v1 §3 all-eight-schema prompt framing | Superseded by §3 state-scoped native schema accounting; 4,096/768 budget/drop order retained |
| DECISIONS v1 §4 qwen3:4b and JSON-schema content | Superseded by §4 Qwen 3.5 native tools; conservative repair + three retries retained |
| DECISIONS v1 §8 terminal-centric workflow | Extended by §8 FastAPI/thin CLI/stateless workers; single language agent retained |
| DECISIONS v1 §9 SQLite WAL / OS singleton worker | Superseded by §9 PostgreSQL-16 advisory transactions, durable claims/fencing and SKIP LOCKED jobs |
| DECISIONS v1 §10 sentence-transformers MiniLM/PyTorch | Superseded by §10 Ollama nomic-embed-text:v1.5, vector(768) in memory row; fixture-vector eval method retained |
| DECISIONS v1 §11 local retry scheduling | Superseded by §11 Postgres due jobs; key/attempt/restart guarantees retained |
| DECISIONS v1 §§1,5–7 | Retained with stronger v2 guardrails and service revision binding; no weakening |
| DESIGN.mdx / DESIGN.plan.html ModelMux visual | Historical/superseded; do not present as current. Regeneration not part of this requested two-document revision |
| §4 Ollama Qwen 3.5 native tools as the runtime model; v2 §3; §6 "cloud LLM out of scope" | Superseded by §17 (user decision, 2026-11-07): ModelMux gateway, Claude Code or Codex. Repair + three retries, scripted offline evals and all guardrails retained |

No application files, Compose configuration, database, model downloads or tests are created/run by this documentation task. Actual model/digest/size, latency, tokenizer counts, database concurrency correctness and embedding relevance remain implementation gates. Markdown design note is compact; exact one-page rendering is a final artifact check, not a claim inferred from word count. PDF was reread for the trace table; no `ASSIGNMENT.md` was fabricated.

## 17. Design Revision v3 — ModelMux replaces Ollama (user decision, 2026-11-07)

**Decision.** At stage 4d the user chose their own [ModelMux](https://github.com/smit153/modelmux/) gateway as the only real-model path, replacing local Ollama Qwen 3.5. Both providers are supported and selected by configuration: `claude` (`http://127.0.0.1:8101/v1`, models such as `sonnet`, `haiku`) and `codex` (`http://127.0.0.1:8102/v1`, account-specific models listed by `/v1/models`). The user was shown the alternatives (keep Ollama as default with ModelMux as a second backend, or install Ollama now) and chose replacement.

**PDF §1 interpretation, and its risk.** PDF §1 says the system "must run locally without real providers, payments, or external service integrations". This design reads that as: no real errand providers, payments or third-party service APIs. Those remain mocked in-process, and nothing in the errand path talks to an external service. The LLM itself is reached through a gateway running on the local machine (loopback only, digest-pinned image), but ModelMux's models run on Anthropic's or OpenAI's servers using the user's own logins. **A reviewer may read this as an external service integration.** That risk is accepted by the user and is stated in DESIGN.md and README. Offline evaluation is unaffected: every automated eval still runs against the scripted model with no network (PDF §4).

**What changes.**
- *Adapter:* `ModelMuxChatModel` speaks OpenAI Chat Completions with `tools`. Action calls send `tool_choice: "required"` and `parallel_tool_calls: false`; ModelMux simulates tool calls and validates them before returning `tool_calls`. Code still revalidates everything (G1/G2/G7/G10 unchanged). The gateway URL must be loopback or a local service name. The gateway key comes from `ERRAND_MODELMUX_API_KEY`, is never logged, and is never committed.
- *G8 determinism:* ModelMux accepts but ignores `temperature`, `seed` and `max_tokens`, and lists them in `X-ModelMux-Ignored-Params`. The app still sends temperature 0 / seed 42 and traces the ignored list on every call, but **real-model runs are not seeded**. Eval determinism always came from the scripted model, which is unchanged.
- *Output cap:* the 768-token output cap can't be enforced through ModelMux, so it is not claimed. Every reply is still bounded by validation, and phrased text by grounding.
- *Cost:* usage tokens are recorded when ModelMux reports them. Cost is "unavailable: subscription-billed via ModelMux"; it is not fabricated.
- *E42 (revised):* the three-example smoke test runs through ModelMux against seeded mocks and an injected clock. It records the first-pass and after-retry valid-call rates, latency per call and per flow, and the provider/model used. The user performs `modelmux login`; the agent never handles credentials.
- *E42 measured (Codex, 2026-11-07):* two runs on `modelmux:codex:gpt-6.1-sol` each completed 3/3 PDF flows with 24/24 calls valid on the first pass, no booking without approval, per-call latency median ≈5.5 s (max 9.7 s) and 27–58 s per flow. Two runs are a smoke check, not a reliability estimate; unseeded real-model behaviour can vary. Claude is not yet measured (not logged in).
- *Removed:* the Ollama chat adapter, which is superseded. The local-only URL check moves to the ModelMux adapter.

**Resolved in §18 (user decisions at stage 5).** Previously open: (1) The *embedding backend*: v2 §5 chose Ollama `nomic-embed-text`, but ModelMux offers no embeddings endpoint. (2) *Token accounting* for the 4,096-token input cap: there is no local tokenizer for hosted Claude/Codex models, and DECISIONS §3 forbids silent chars-per-token assumptions.

## 18. Stage-5 decisions — embedding backend and context-cap accounting (user decisions, 2026-11-07)

**Embeddings: local Ollama, embeddings only.** §10 stands as written: `nomic-embed-text:v1.5`, 768 dimensions, loopback `/api/embed` with `truncate: false`, the `search_query: ` / `search_document: ` prefixes, and the full digest and runtime version pinned once installed and verified. Ollama serves embeddings only; chat stays on ModelMux (§17). Because the embedder runs on this machine, memory retrieval adds no external dependency. Ordinary evals still use checked-in fixture vectors. The real-embedder pair check is a separate, explicitly selected integration test that fails clearly when Ollama or the model is missing and never downloads anything. The alternatives the user was shown were an in-process ONNX model, deterministic lexical vectors, and Postgres full-text search.

**Context cap: a byte bound on what the app sends.** PDF §3 requires a fixed context cap. With hosted Claude/Codex models there is no local tokenizer, and §3 forbids assumed chars-per-token ratios. The cap is therefore enforced as **at most 4,096 UTF-8 bytes of app-assembled input**: the serialized messages plus the offered tool schemas, exactly as sent. For byte-level BPE tokenizers every token encodes at least one byte, so this guarantees at most 4,096 tokens of app content without a tokenizer. The §3 allocation table and drop order are kept and read in bytes. A request that still exceeds the cap after the drop order is never sent; it fails with an explicit outcome (E22).

**Measured gateway overhead is outside the cap and is stated, not hidden.** ModelMux adds its own CLI system prompt and tool-calling framing: in the Codex smoke test, a 251-byte phrase request was reported as 4,532 prompt tokens, and the app's own content peaked at 1,922 bytes. The provider-reported `prompt_tokens` stays in every model-call trace. The cap claims only what the app controls. The 768 output cap remains unenforceable through ModelMux (§17).

The alternatives the user was shown were a proxy tokenizer (tiktoken o200k) and enforcement from provider-reported tokens after the call.

**Implementation notes (stage 5a/5b, within §§7, 10).** Stored fields are typed: preferred area, earliest acceptable time (global or per service), preferred provider per service, and short notes in the user's own words. Inspect, confirm, correct and forget are deterministic commands handled by code, like `yes` / `confirm <token>`: they touch only the caller's own memory and never reach the model. Explicit "remember …" is extracted by the model but accepted only when the user's message contains explicit memory wording (and a note only verbatim), so neither the model nor review text can write memory. Saved preferences are applied as visible defaults recomputed each turn rather than copied into the request, so there is no derived state to invalidate on correct/forget. A saved provider never bypasses a filter; it only orders first among eligible options. Inference rules: the last 3 completed, approved bookings of a service with one provider → provider candidate; the last 3 overall all starting ≥ 09:00 → earliest-time candidate; candidates for a forgotten field+scope are never re-created.

## Appendix A — v1 decisions (superseded historical record)

<details>
<summary>Original v1 log; current sections above take precedence</summary>

# Design review decisions

7 November 2026. These are adopted design decisions and planned acceptance checks, not implemented behavior. Numbers below are explicit design assumptions or counts of the proposed workflow, not measurements of an application. The assignment's eight tool signatures and failure behaviors remain unchanged.

## 1. Operation keys versus dispatch attempts

| Option | Advantages | Costs and failure modes |
| --- | --- | --- |
| New idempotency key on every dispatch | Simple attempt accounting; independently retryable calls | Unsafe: a timeout after success followed by a new key can duplicate the booking. Reject. |
| One key for the whole conversation or booking lifecycle | Easy deduplication | Conflates booking, cancellation, reschedule, and revised proposals; can replay the wrong result or prevent legitimate later work. Reject. |
| Stable operation key plus separate attempt IDs | Deduplicates one exact approved action while retaining every transport attempt; supports restart recovery | Requires a durable operation journal and explicit terminal/nonterminal outcomes. Adopt. |

An operation is an immutable action snapshot linked to one approved proposal revision. Generate and persist its opaque key once with approval consumption, before dispatch. Each invocation gets an attempt ID and ordinal. A unique proposal-to-operation constraint makes repeated confirmation return the existing outcome. Never derive a key solely from provider/slot: a legitimate later action may share those fields.

Retrying the same intent after a transient failure uses the same key. A different hold, price, slot, provider, or action requires a new proposal, fresh approval, and new operation/key. Resolve any old ambiguous operation before starting a replacement that could duplicate its effect. Terminal `slot_taken`/`hold_expired` results do not become success by rotating keys. The mock backend persists committed effects and replayable success together; a known no-effect transient failure does not permanently cache the operation as failed. Failure sampling is configurable per dispatch, while committed outcomes replay without resampling.

Planned checks: repeated confirmation; crash before/after provider commit; timeout without success then same-key retry; changed proposal; terminal failure replay; no duplicate effect across attempts.

## 2. Counted model-call budget

Use one structured model decision at each row; deterministic application transitions and fixed outcome messages require no model call. No hidden summarization or memory-extraction calls are allowed.

| Step | Model decision | Following work |
| --- | --- | --- |
| 1 | Parse initial incomplete request and ask only for missing fields | Wait for user; run ends |
| 2 | Merge reply and propose search | `search_providers` |
| 3 | Compare candidates and request selected provider details | `get_provider` |
| 4 | Assess details/reviews and propose a slot hold | `hold_slot` |
| 5 | Read actual held quote and propose exact approval presentation | Code renders immutable proposal; wait for user; run ends |
| 6 | After code validates confirmation, propose booking | Code rechecks approval; `book` |
| 7 | Interpret booking result and request authoritative status | `get_booking_status`; code renders verified outcome |

Total: **7 logical decisions, 5 tools**, for one clarification and one selected candidate. A fully specified initial request skips step 1: **6**. “Same provider as last time” adds one model decision and `list_bookings`: **8 decisions, 6 tools**. A timeout-without-success followed by one retry adds another `book` and status lookup, both prescribed by code: no extra model call. More candidate inspections or user corrections increase these counts; they are not assumed free.

The previous six-model-call cap does not cover this seven/eight-decision conversation if interpreted end-to-end. Its original per-run definition does cover the ordinary middle segment (steps 2–5: four); this distinction was missing. A run starts on user/event/resume input and ends at a user wait, terminal result, or safe checkpoint. User waiting consumes no execution time. A new run cannot reset operation retry limits.

Adopt **10 logical decisions, 40 physical inference calls, 20 tool invocations, and 300 seconds active time per run**. Each logical decision permits initial generation plus three retries, so 10 × 4 = 40; local syntax repair uses zero inference calls. The counted seven/eight-decision flow would use at most 28/32 physical calls if every decision succeeds only on the fourth generation. These bounds fit the counted work; they do not promise CPU speed. Deadline can terminate earlier. Use at most 60 seconds per model call, bounded by remaining time, including cold loading. Both model and tool dispatch reserve a durable counter before starting; failures consume it.

For one consequential operation, persist a lifetime allowance of **3 side-effect dispatches, 6 reconciliation calls, 40 model calls, and 300 seconds charged active time**, shared across restarts; run caps also apply. Record dispatch deadline before a call; after crash charge its outstanding reservation conservatively. Recovery stops safely when exhausted. Read-only inspection remains possible; retry budget does not silently reset on restart or repeated confirmation. Explain pending versus known failure. A materially revised operation needs a resolved predecessor and fresh approval.

Planned checks assert the exact ordinary/history sequence and counters, invalid-output worst case, repeated clarification across runs, and exhaustion on either run or operation cap. No benchmark has been run.

## 3. Context allocation and drop order

Assume short English requests, 3–8 compact candidates, at most six relevant short preferences, and a compact typed action schema. Adopt **4,096 input tokens**, **768 output tokens**, local runtime context **8,192**. Input allocations are engineering estimates to validate with the pinned tokenizer during implementation.

| Component | Input tokens |
| --- | ---: |
| System policy, trust, approval and recovery rules | 600 |
| Eight tool signatures and structured action schema | 850 |
| Current request, proposal revision, operation and approval facts | 550 |
| Current user message | 350 |
| Current structured tool results/candidates | 700 |
| Eligible relevant memories | 350 |
| Quoted untrusted review excerpts | 150 |
| Retry diagnostics and previous invalid output excerpt | 300 |
| Chat template, delimiters, and accounting reserve | 246 |
| **Total** | **4,096** |

This is a maximum allocation, not an instruction to pad requests. Count actual tokens over the final rendered template, including schema and repair instructions, before every original/retry call. Verify tokenizer/template agreement with runtime prompt usage; never rely on characters-per-token or silent runtime truncation. Pin tokenizer, model digest, and template. The unused context capacity is safety space, not permission to exceed the input cap.

Drop in order: (1) review excerpts, (2) lowest-ranked optional memories, (3) excess unselected candidate descriptions and old nonessential tool details, (4) shrink retry excerpts to a bounded diagnostic. Preserve current candidate IDs/material facts required for the decision. Never drop policy, schema, current user intent, active proposal, price, expiry, approval revision, or unresolved operation status. If essentials still exceed the cap, dispatch no model call; explain that the request is too long and ask for a shorter input. Do not truncate the user's correction silently. No raw conversation, obsolete fact, forgotten fact, or unconfirmed inference is a budget candidate at all.

Planned checks: 50+ seeded memories and long history; exact context exclusions; each drop stage; oversized essential input; retry prompt stays within cap.

## 4. Local inference and invalid JSON

Adopt Ollama on loopback with local `qwen3:4b`, initially a modest quantized candidate rather than a quality/speed guarantee. Pin downloaded model digest and runtime version before evaluation; no cloud fallback. Download dependencies/weights once during setup, then demonstrate disconnected runtime. The published [Qwen3 tags](https://registry.ollama.com/library/qwen3/tags) list a roughly 2.5 GB 4B artifact; runtime memory and latency need a real smoke test. ModelMux remains separately documented infrastructure, not the assignment model path.

Request a JSON schema, disable thinking where the pinned model supports it, and validate syntax, schema, allowed actions, arguments, state and authorization in code. Ollama documents schema output and application validation in its [structured-output guide](https://docs.ollama.com/capabilities/structured-outputs), and the [chat API](https://docs.ollama.com/api/chat) exposes usage counts. Structured output reduces malformed replies; it does not confer approval or guarantee valid actions.

On invalid JSON: first perform conservative local syntax repair (strip one enclosing code fence or surrounding whitespace; never invent values, approvals, or fill truncated content). Revalidate. If still invalid, make up to **3 additional model generations**, each with bounded validation diagnostics; locally repair and validate each response. This means at most **4 inference calls per logical decision**, not three total and not an extra hidden repair-model call. Semantic/schema failures use the same bounded retry path but cannot be fixed by inventing facts. Global limits may stop retries earlier. Never execute output until all gates pass.

Trace original generation, each syntax-repair attempt, validation failures, retry ordinal, sanitized input/output, latency and token usage. Do not log hidden reasoning. Final message: “I couldn't produce a valid action after three retries. No new action was executed from that response. Please try again or simplify the request.” If there is already an unresolved operation, append its truthful pending status instead of suggesting it failed. Deadline/limit exits state that actual reason and attempt count.

Planned checks: local repair succeeds; third retry succeeds; all fail; semantic invalidity; injected approval; truncated output; deadline interrupts retry; every attempt traced. Scripted model drives offline evaluations.

## 5. Expired holds and changed prices

Revalidate current hold and material facts immediately before consuming approval/dispatch. The in-process mock backend also checks hold expiry atomically when booking; code cannot eliminate that race with an earlier check alone. Never silently transfer approval to a replacement hold or price.

Expired: “The hold for Tuesday at 10 a.m. expired, so I haven't booked it. I'll check availability again. Any new hold will need your approval.” Show a new exact proposal only after obtaining a valid hold.

Price change: “The quote changed from ₹1,200 to ₹1,350. Your earlier approval no longer applies. Would you like to book this slot for ₹1,350? Reply yes or confirm <new token>.” If over budget, ask whether to change the budget or search alternatives before offering an actionable proposal. A hold quote different from search price is displayed before the first approval; it is not treated as approval of the search estimate. A timeout stays in reconciliation, not this known-no-booking path.

Planned checks cover expiry before approval and during dispatch, price change before/after approval, over-budget quote, and no side effect under stale approval.

## 6. Explicit token and plain yes

Accept a normalized standalone `yes` (case/outer whitespace ignored) and `confirm <token>` from authenticated user chat input only. Other phrasings may clarify, but cannot authorize by model interpretation. Each inbound message carries the revision of the latest proposal actually displayed in that session when input began; persist that association. A terminal implementation records it at prompt display, not after asynchronous events arrive.

Both forms must target the current displayed revision, unchanged request revision and material snapshot, valid expiry, correct user/session, and pending status. Only one actionable proposal may be displayed at once. If facts change while a user types, reject the response, explain the change, display a new revision, and obtain a new response; never bind the old yes to an unseen replacement. Code handles confirmations before model input. Tokens do not bypass stale-revision checks. A duplicate confirmation for an already consumed unchanged proposal returns its existing operation outcome; it never dispatches a second operation.

Planned tests for **each form**: valid booking/cancel/reschedule; stale price/provider/slot/hold; expired proposal; duplicate/restart replay; correction in flight. Additional plain-yes tests: no proposal, multiple notifications, cross-session input, and a new revision arriving while typing. A model-generated yes or a yes embedded in a review is rejected. No tests written yet, per design-only scope.

## 7. Who creates inferred memories?

| Approach | Advantages | Costs and injection risk |
| --- | --- | --- |
| Model infers from conversation | Handles paraphrases and nuanced preferences; broader coverage | Can turn quoted instructions or malicious reviews into persistent “user preferences”; opaque rationale, extra inference, drift, hard replay. Filtering prompts alone cannot prevent memory poisoning. |
| Code infers from structured user choices | Auditable triggers, no extra model call, reproducible tests, provenance can exclude provider text | Narrower coverage; thresholds are heuristic; repeated situational choices can be mistaken for stable preference. |
| Hybrid candidate generation with code validation | Flexible model suggestions with typed records and provenance checks | More components; source validation still cannot prove a plausible inference is true; requires user confirmation regardless. |

Adopt **code-generated candidates** for this assignment: for example, three explicit morning selections in distinct completed errands produce an unconfirmed candidate, not an active preference. Count only user selections tied to durable outcomes; do not count auto-ranked suggestions, duplicate retries, provider text, or model assertions. An inference cannot establish permissions, approval, payment behavior, or tool policy. The model can extract an explicit statement from the current user message into a typed candidate, but code verifies source and asks confirmation if ambiguous.

Store source event IDs, scope, confidence/reason, explicit/inferred origin, confirmation, revision and tombstone. Show unconfirmed candidates only in a separate deterministic memory-management view; they never reach model context, retrieval query, or summaries. Ask separately whether to remember one; booking yes is not memory confirmation. Once user-confirmed, retain inferred origin plus confirmation metadata. Forgotten facts and their embeddings/derived summaries are removed from active retrieval; traces are audit data and never retrieval sources. This trades recall for a small, defensible injection surface.

## 8. Is another agent worth it?

A separate cancellation agent could isolate a queue and specialize replacement search, and might help at large volume. It would also need its own model budget, duplicate-event handling, access to preference changes, and coordination over proposal revision, approval, and operation ownership. An additional model cannot replace those application invariants. For one local user and eight fixed tools, the added coordination and debugging paths outweigh specialization.

Adopt **one LLM agent plus a deterministic background watcher**. The watcher consumes durable simulated cancellation events, updates booking state, invalidates affected proposals and queues a notification/replacement task through the same workflow. It does not create approval or automatically rebook. Notification deduplication uses a unique event/user outbox key; an inbox item survives restart and supports at-least-once delivery with stable IDs, rather than claiming exactly-once terminal display. No-match results explain the limiting preference instead of relaxing it. A second LLM agent can be reconsidered only if measured workload or independent expertise justifies it.

## 9. Locking and concurrency

| Strategy | Advantages | Costs |
| --- | --- | --- |
| In-memory session mutex only | Simple and fast | Does not protect across processes/restarts; insufficient alone. |
| Database transaction held across all work | Easy apparent serialization | Blocks watcher/UI while inference runs, causes lock timeouts, still cannot make an external side effect transactional. Reject. |
| Short SQLite transactions plus optimistic revision checks and durable claims | Restart-safe, protects invariants, leaves reads responsive | Must handle busy errors, stale results and claim recovery explicitly. Adopt. |

For the local assignment, run one operation worker under an OS advisory lock for this database; watcher and chat submit work to it. The lock is released on process death; a second worker fails clearly instead of racing. Use SQLite WAL and bounded busy timeout (2 seconds, also bounded by run deadline). `BEGIN IMMEDIATE` covers approval validation/consumption, unique operation insertion and dispatch reservation; commit before any tool/model call. Revision compare-and-swap rejects stale results when the watcher changes the request during inference. Unique constraints protect proposal consumption, operation keys and cancellation-event ingestion. Mock side effects and their replay record commit together in a short backend transaction.

After crash, obtain the worker lock, inspect persisted in-flight work and reconcile before dispatch; never steal a timed lease while its old worker might still act. This intentionally favors a simple singleton over distributed leases/fencing. The cost is no multi-worker throughput, acceptable for this assignment. Never hold a session mutex while waiting for user input. Planned race tests interleave confirmation, cancellation, price change and duplicate dispatch; separately test two worker processes and crash release.

## 10. Local embeddings and reproducible ranking

Adopt **sentence-transformers/all-MiniLM-L6-v2** running locally on CPU. Its [model card](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) describes 384-dimensional embeddings and truncation beyond 256 word pieces. It is a compact English retrieval baseline; English evaluation is an assumption, and multilingual requests need explicit later evaluation rather than a claimed capability.

Download weights/tokenizer during setup; pin immutable repository revision, artifact hashes, sentence-transformers/PyTorch versions, and preprocessing in an embedding manifest before implementation acceptance. No invented pin is claimed here. Use evaluation mode, CPU, one thread, deterministic algorithms, fixed normalization, and no dropout. Reject or explicitly segment long facts before encoding; prefer one short typed preference per record. Query derives only from current structured user intent; never include raw reviews or excluded memories.

Filter by user, scope, current revision, not forgotten, confirmed, and applicable preference field **before ranking**. Then compute normalized vectors, cosine similarity, sort by score rounded to six decimals descending and stable memory ID ascending. Include at most six applicable memories within 350 tokens; selected threshold initially 0.30 is a tunable hypothesis to freeze after labeled relevance fixtures. Required current constraints live in state, not probabilistic retrieval. Similarity is not a trust check; test exclusion of confirmed but irrelevant memories too.

Offline evaluations use checked-in fixture query/memory vectors produced by that pinned local model, with input hashes and model manifest; real ranking/filtering/budget code still runs. A separate network-disabled local integration check encodes fixtures, checks numerical tolerance and expected ordering, and fails clearly if pinned artifacts are absent; no remote auto-download. CPU libraries can differ slightly across platforms, so do not promise cross-platform bitwise floats: fixed fixture vectors guarantee deterministic scenario ranks, while the pinned environment and margin-separated fixtures validate real encoder behavior. Test near ties explicitly with the stable tie rule. Corrections/forgetting invalidate stored vectors and ranking caches atomically; a stale fixture hash is an error.

## 11. Durable cancellation 503 retries

Persist operation key, booking ID, approval snapshot, state, dispatch reservations, attempt ordinals, retry count, last sanitized error, next_retry_at, and lifetime budgets. Mock backend keeps the per-operation first-503 state durably under the same key; it must not reset that behavior on process restart. The required behavior is first call 503 with no effect, next dispatch succeeds. Transient 503 is an attempt result, not the operation's terminal replay result.

State sequence: `approved → executing(attempt 1) → retry_wait(503, due +1s) → executing(attempt 2, same key) → succeeded`. If additional configured transient faults exist, permit a third dispatch after 2 seconds, then stop with a known failure or pending ambiguity as appropriate. Three total dispatches is a lifetime maximum, not three on every restart. No jitter in the assignment; use a fake clock for tests. Waiting is checkpointed, not a blocking sleep holding locks. A resume run has its own cap and the existing operation's remaining allowance.

On restart, resume a durable due retry, or leave it queued until due. If the crash happened after the backend recorded 503 but before the client recorded it, the backend's durable attempt state still makes the next same-key call succeed. If cancellation might already have succeeded, use `list_bookings(user_id)` to inspect that booking (the fixed status tool is for booking holds/keys), then replay the same cancellation key if necessary within budget; successful replay must not cancel twice. Authoritative cancelled state can also come from provider cancellation: report the observed state without falsely attributing it to this call. If status is unavailable and retry budget is exhausted, say cancellation is pending verification.

Planned restart tests: before/after first 503, before due time, after retry success but before client persistence, exhausted attempts, backend restart, and cancelled-by-provider race. Assert same key, persistent 503 state, bounded attempts and truthful outcome.

## Evidence and document status

DESIGN.md is the compact design note; this log contains the full review reasoning. The earlier DESIGN.mdx/HTML ModelMux visual is superseded and must be regenerated before presentation as current design. No app code, tests, model installation, provider login or live inference was performed for this review. The actual trace excerpt and evaluation table remain implementation deliverables.

</details>

## Appendix B — v1 design note (superseded historical record)

<details>
<summary>Original compact design; do not implement from this archive</summary>

# Errand Agent — design

**Design only · reviewed 7 November 2026.** The PDF is authoritative; preserve all eight mock tool contracts. Detailed trade-offs, budgets, and planned checks are in [DECISIONS.md](DECISIONS.md). No implementation or passing evaluations are claimed.

**Architecture.** One Python conversational agent, terminal chat, SQLite, in-process mocks, and local Ollama `qwen3:4b` inference. Download and pin model artifacts during setup; runtime and scripted evaluations require no external services. ModelMux setup remains separate infrastructure and is not required by this assignment. A deterministic watcher detects provider cancellations and invokes the same workflow; another LLM agent adds coordination cost without a distinct required capability.

**State and approval.** Persist `collecting → comparing → held → awaiting_approval → executing → verifying → completed`, with conflict, retry, and pending-recovery branches. Corrections retain unrelated fields. Show an immutable proposal revision containing action, provider, slot, price, target hold/booking, and expiry. Application code accepts `confirm <token>` or a standalone `yes` bound to the latest actually displayed revision. Atomically reject changed/stale facts, consume approval, and claim the operation. Expired holds and changed prices get a specific explanation and a fresh proposal requiring fresh approval. Model/tool text cannot authorize actions.

**Keys, locking, recovery.** One durable idempotency key per approved immutable operation; distinct attempt IDs per dispatch. Transient retries reuse the key; materially changed, newly approved actions receive a new operation/key only after the old outcome is resolved. SQLite WAL, short `BEGIN IMMEDIATE` transactions, revision checks, unique constraints, and a singleton worker lock serialize claims; never hold a transaction during inference/tool work. After crash or booking timeout, reconcile by key before retrying or claiming success. Persist the mock backend's effects and idempotency records atomically. A cancellation 503 is a nonterminal attempt: persist attempt count and next retry time, then retry the same key after restart. Bound recovery across restarts; ambiguous outcomes stay pending.

**Limits and output.** The explicit complete booking path uses seven model decisions (eight with booking-history lookup), spanning approval-separated runs. Allow ten logical decisions and forty physical model calls per run, twenty tool calls, and 300 seconds active time; also cap an operation across restarts. For invalid JSON, first attempt a conservative local syntax repair, then make up to three model retries; validate schema and semantics each time, trace every attempt, and fail visibly without executing invalid output. Reserve 4,096 input tokens plus 768 output tokens inside an 8,192-token local context. Tokenize the complete rendered prompt; drop optional reviews, low-ranked memories, then extra candidate detail. Never remove safety rules or current action facts to fit.

**Memory and trust.** Code creates inspectable, unconfirmed inferred preferences from auditable structured user choices; the model may parse explicit user statements but cannot promote inferences. Confirmation is separate from booking approval. Only current, confirmed, relevant facts survive provenance/scope filters, then local `all-MiniLM-L6-v2` embeddings rank them. Forget/correct invalidates vectors and derived state. Pin embedding artifacts and CPU dependencies; offline tests use recorded vectors plus a local embedding integration check. Untrusted reviews never become instructions, memories, or permissions. No raw conversation replay.

**Proactive behavior and evidence.** A durable cancellation inbox/outbox deduplicates events, notifies affected users, searches within current preferences, and requires fresh approval for replacements. Trace states, IDs, exact model context, each model/repair/tool attempt, sanitized results, latency, usage, and unavailable costs explicitly. Plan at least twenty offline scenarios asserting outcomes and invariants, including both approval forms, races, retries/restarts, injection, memory exclusion, and limits. Deliver generated eval results, three real bugs with regression tests, transcript, README, and a 3–5 minute demo. **Trace excerpt pending implementation:** the required real timeout-after-success trace will show persisted key → book timeout → authoritative status confirms booking → success; this description is not execution evidence.

</details>
