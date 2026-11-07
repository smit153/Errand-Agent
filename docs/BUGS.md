# Real bugs hit while building

**The three highlighted for the assignment (PDF §5)** — each found while building, fixed, and guarded
by a regression test that fails without the fix:

1. **BUG-002 — a timed-out booking could never be retried.** An attempt whose outcome was unknown was
   never marked reconciled, so after the status check said "not booked" the same-key retry never
   happened. Fixed with an explicit `reconciled` attempt state (migration 0002).
2. **BUG-008 — a status-service outage spent every verification at once.** The executor re-checked
   `get_booking_status` in a tight loop until all 6 lifetime checks were gone, leaving a possibly-made
   booking unverifiable. Now 2 per attempt, then a queued follow-up every 30 s.
3. **BUG-007 — a retry job blocked its own continuation.** A second 503 on a background cancel left
   the operation in `retry_wait` with nothing scheduled to retry it. Jobs now hand over to their
   successor in the same transaction.

All bugs found so far follow, in the order they were hit.

Each entry was caught by an automated test that failed before the fix and passes after it; that
test now guards against regression. Entries are added as bugs are encountered, not reconstructed.

## BUG-001 — cancel retry scheduled after 2 s instead of 1 s

- **Found:** 2026-11-07, stage 3b, by `tests/test_executor.py::test_cancel_503_schedules_same_key_retry_then_verifies`
  (`next_retry_at` was `now + 2s`; the design says the first 503 retry is due after 1 s).
- **What broke:** after the required first-call 503, the operation's retry was scheduled with the
  *second* back-off step.
- **Why:** `_record_dispatch` computed the delay from the `_Op` snapshot loaded *before* the attempt
  was reserved. Its `dispatch_count` was still 0, so `delays[min(0, n) - 1]` became `delays[-1]`,
  Python's last element, instead of raising. Stale in-memory state was trusted over the durable
  attempt row.
- **Fix:** read the reserved attempt's own `ordinal` from `operation_attempts` inside the recording
  transaction and index the back-off table with it (`errand_agent/workflow/executor.py`).
- **Regression test:** the test above asserts `next_retry_at == now + 1s`, that nothing is sent
  before it is due, and that the due retry reuses the same idempotency key.

## BUG-002 — verified "not booked" never led to the same-key retry

- **Found:** 2026-11-07, stage 3b, by
  `test_timeout_without_booking_checks_status_then_retries_same_key` (1 `book` call instead of 2)
  and `test_dispatch_lifetime_cap_is_three_and_ends_verified_not_booked` (ended
  `pending_verification` instead of `retries_exhausted`).
- **What broke:** after `book` timed out without booking, `get_booking_status` correctly said
  `not_booked` and the operation moved back to `executing`. The executor then reconciled again and
  again instead of retrying, until the 6-reconciliation lifetime cap ended it as
  `pending_verification`. This was safe (no duplicate, nothing claimed) but wrong: the approved
  booking was never completed.
- **Why:** the timed-out dispatch attempt stayed in status `unknown` after the authoritative check
  had resolved it. `_needs_reconcile` treats any `unknown` dispatch as unresolved, so every step chose
  reconciliation.
- **Fix:** when a reconciliation records an authoritative observation, mark all earlier `unknown`
  dispatch attempts `reconciled` in the same transaction. Migration `0002_attempt_reconciled.sql`
  adds the status to the CHECK constraint.
- **Regression tests:** the two tests above; they assert exactly two (or three) `book` calls with
  one stable key and distinct attempt IDs, and a verified final outcome.

## BUG-003 — forced status checks were missing from the operation's trace

- **Found:** 2026-11-07, stage 3b, by `test_success_is_reported_only_after_authoritative_status`
  (`('tool.call', 'get_booking_status') is not in list` for the operation's trace events).
- **What broke:** the forced `get_booking_status` / `list_bookings` reconciliation calls were traced
  with `operation_id = NULL`. An operation's trace showed the `book` call and the success, but not
  the authoritative check in between. That undermines the PDF's required timeout-after-success trace.
- **Why:** `ToolClient` took the trace's `operation_id` only from the fence, and reconciliation
  calls are read-only, so they carry no fence.
- **Fix:** `ToolContext.operation_id` carries correlation independently of fencing; the executor
  sets it on reconciliation calls, and the client uses it when there is no fence.
- **Regression test:** the test above asserts the order `book` → `get_booking_status` →
  `operation.finished` within the operation's trace.

## BUG-004 — single-value ID enums reached the model as `const`, not `enum`

- **Found:** 2026-11-07, stage 4c, by `tests/test_agent_scenarios.py::test_e01_plumber_asks_only_for_area_then_books_after_approval`
  (`KeyError: 'enum'` reading the offered `ask_user` schema).
- **What broke:** most per-turn tools offer exactly one legitimate value (the code-ranked option, the held
  slot, the approved hold). Pydantic renders a one-value `Literal` as `{"const": ...}`, so those ID
  fields didn't carry the explicit list of allowed values that G2 specifies. Validation still
  rejected wrong values, but the model was shown a different, less explicit schema shape than intended.
- **Why:** `ToolDef.spec()` passed Pydantic's JSON schema through unchanged; one- and many-value
  enums produce different keywords.
- **Fix:** `spec()` normalizes `const` to a one-element `enum`, so every ID field reads as a list of
  allowed values (`errand_agent/llm/toolspec.py`).
- **Regression test:** the E01 scenario asserts the offered `ask_user.fields` enum is exactly `["area"]`;
  `test_valid_call_is_accepted_and_fully_traced` asserts the provider enum in the logged schema.

## BUG-005 — "show what I have so far" dismissed the degraded-mode menu

- **Found:** 2026-11-07, stage 4c, by `test_e30_degraded_menu_keeps_collecting_safely`
  (`search_providers not offered`: the direct `area: Indiranagar` entry went to the model instead).
- **What broke:** after repeated model failures the user picked the informational "status" item.
  The menu was cleared, so the next deterministic `area: …` entry was treated as normal input and
  sent back to the failing model. The degraded path was lost, though nothing unsafe happened.
- **Why:** `_menu` cleared the menu before dispatching on the chosen item, including for the one
  item that doesn't change anything.
- **Fix:** "status" replies and returns early with the menu still active; only acting items clear it.
- **Regression test:** the test above picks "2" (status) and then completes the request via `area: …`
  without any extraction call.

## BUG-006 — the smoke runner read the wrong ModelMux key variable

- **Found:** 2026-11-07, stage 4d-b, on the first real run of `scripts/smoke.sh codex`
  (`no MODELMUX_API_KEY in ~/.config/modelmux/secrets.env`).
- **What broke:** the real-model smoke test could not start. Nothing reached the model, and no key
  was printed.
- **Why:** the script assumed the variable was `MODELMUX_API_KEY`. ModelMux actually writes exactly
  one line, `MODELMUX_API_KEYS=<base64url, ≥ 32 chars>` (modelmux-cli `secretsStore.js`). This was
  written from memory of the docs, not checked against the CLI's source.
- **Fix:** key extraction moved to `scripts/modelmux_key.sh`, which accepts only that exact
  one-line format and rejects anything else with an error that never echoes the file.
  `smoke.sh` calls it.
- **Regression test:** `tests/test_smoke_scripts.py` feeds fake secrets files: the real format is
  read; the singular name, a short value, extra lines, a quoted value and an empty file are rejected
  with no output and the key absent from stderr.

## BUG-007 — a retry job couldn't schedule its own continuation

- **Found:** 2026-11-08, stage 6a, while wiring the job worker to the executor (review of the
  retry path), then pinned by a test.
- **What broke:** when a background cancel retry hit another transient 503, the executor tried
  to queue the next retry, but the job running it was still claimed and counted as "an open retry
  job for this operation". No continuation was queued, so the operation would have sat in
  `retry_wait` with nothing left to run it. The same applied to a reconcile job re-queuing itself.
- **Why:** de-duplication ("one open job per kind and operation") didn't distinguish the running
  job from a genuinely separate open one.
- **Fix:** `queue.enqueue(..., handover_from=holder)` closes the caller's own claimed job of the
  same kind for that operation and inserts its successor in the same transaction as the state
  change. As a safety net, the worker re-queues a retry for any operation it leaves in
  `retry_wait` without one.
- **Regression test:** `tests/test_jobs.py::test_bug007_a_second_503_hands_over_to_a_second_retry`
  (two 503s → second retry queued and run → succeeded with exactly 3 dispatches, no open jobs left).

## BUG-008 — a status-service outage burned the whole reconciliation allowance at once

- **Found:** 2026-11-08, stage 6a, by `test_pending_verification_is_followed_up_until_resolved`
  (no follow-up job was queued after a booking timeout).
- **What broke:** after a booking timeout with `get_booking_status` failing, the executor's loop
  re-checked immediately until all 6 lifetime reconciliations were spent, within milliseconds.
  Nothing was left for a later check, so a brief status outage left a possibly-made booking
  unverified for good (truthfully "pending", but needlessly).
- **Why:** a failed status check set `pending_verification`, and the drive loop treats that state
  as "reconcile now", with no per-drive bound other than the lifetime cap.
- **Fix:** at most 2 failed status checks per drive (`STATUS_FAILURES_PER_DRIVE`); the drive then
  stops with a pending outcome and a reconcile job is queued 30 s later, until the lifetime cap.
  The final "still can't confirm" notice is sent when follow-ups stop.
- **Regression tests:** `tests/test_executor.py::test_status_service_down_exhausts_reconciliations_and_stays_pending`
  (2 checks per drive, a follow-up queued, 6 in total across drives) and
  `tests/test_jobs.py::test_pending_verification_is_followed_up_until_resolved` /
  `test_follow_ups_stop_at_the_lifetime_limit_and_say_so`.

## BUG-009 — the trace under-reported embedding requests

- **Found:** 2026-11-08, stage 7b, reading the real timeout-after-success trace: the run made an
  embedding request (`memory.retrieved`) but `run.ended` reported `embedding_calls: 0`.
- **What broke:** the per-run cap of 4 embedding requests was enforced inside the embedder
  wrapper, but the run's durable `embedding_calls` counter was never charged. The limit held, but
  the trace's usage figures were wrong, which matters because the PDF asks the trace to report
  usage.
- **Why:** stage 5c added an in-memory budget for embeddings, alongside the run counters,
  instead of going through `reserve_run` like model and tool calls.
- **Fix:** `BudgetedEmbedder(charge=...)` reserves the run's `embedding_calls` counter for each
  request (a refusal becomes "embedder unavailable", so memory degrades explicitly).
- **Regression test:** `tests/test_context.py::test_e22_at_most_four_embedding_requests_per_run`
  now asserts the run counters equal the embedder's actual request count (verified to fail
  without the fix).

## BUG-010 — restating a saved default threw away the approval card

- **Found:** 2026-11-08, stage 8, in a live run with the real model through the CLI: the saved
  area (Indiranagar) was applied as a default, then the user typed "Indiranagar".
- **What broke:** the extraction set `area`, which differed from the stored request (where the
  default is deliberately not written), so it counted as a material change. The card was
  invalidated and re-planned. Worse, our own 10-minute hold on the cheapest slot then made that
  slot unavailable, so the user got a pricier card (₹850 instead of ₹600) for restating what was
  already in effect.
- **Why:** "changed" compared against the stored request, not against what the displayed card
  was built from.
- **Fix:** while a card is pending, a field set to exactly the value in the card's stored
  constraints is not a change (`Agent._restated`). Genuine changes still invalidate the card.
- **Regression test:** `tests/test_memory_scenarios.py::test_bug010_restating_a_saved_default_keeps_the_card`
  (same receipt still valid, no extra hold, one pending card; verified failing before the fix).
- **Known limit, recorded in DECISIONS §19:** after a genuine change, our own earlier hold can
  still hide that slot until it expires (there is no release-hold tool in the fixed contracts).
