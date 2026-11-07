# Real bugs hit while building

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
