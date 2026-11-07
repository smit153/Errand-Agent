# Demo script (3–5 minutes)

What the recording must show (PDF §5): one full booking, chat and trace side by side; a strong plus
is a failure handled live. This script shows a booking whose `book` call **times out after it
actually succeeded**, then a **provider cancellation** handled without a user prompt.

## Setup (before recording)

```bash
docker compose up -d --wait postgres
docker compose --profile embed up -d --wait ollama        # memory/embeddings (optional but shown)
modelmux up                                               # ModelMux gateway; provider logged in
scripts/serve.sh codex --db errand_demo --fresh --demo-controls --seed-memories
```

Then open two terminals side by side, both in the repository root:

| Left: chat | Right: live trace |
| --- | --- |
| `python3 -m errand_agent.cli chat` | `python3 -m errand_agent.cli trace --follow` |

A third small terminal is used for the demo controls (they steer the mocks, not the agent).

## Script

1. **The request (≈40 s).** Type: `Find a plumber for my kitchen tap this week, mornings only, under ₹1,500.`
   The agent asks only for the area. Right pane: the `extract` model call, the per-state tools
   offered, the saved preferences retrieved (the seeded memory includes a preferred area, so it may
   be applied as a visible default).
2. **Comparison and card (≈40 s).** If asked, type `Indiranagar`. Point out: the ranking and window
   are computed by code; the provider reviews (one has a prompt injection) went to the model only
   inside an `<untrusted_data>` block; the card shows the held price, hold expiry and a token.
3. **Arm the failure (≈10 s).** Optional: in the chat, `/quit` and `chat --session N` to show the card is shown again on reconnect. In the third terminal: `python3 -m errand_agent.cli demo next-booking timeout_booked`.
4. **Approve (≈60 s).** Type `yes`. Right pane, in order: `operation.created` with the idempotency
   key → `model.call act` proposing `book` → `guardrail.snapshot_check` accepted →
   `tool.call book ERROR timeout (effect unknown)` → `executing → verifying` →
   `tool.call get_booking_status → booked` → `operation.finished succeeded, verified`. The chat
   says "Booked and verified" only after that check. Say `yes` again: same booking, no second call.
5. **Provider cancels (≈60 s).** `python3 -m errand_agent.cli demo bookings` to copy the new booking ID, then
   `python3 -m errand_agent.cli demo provider-cancel BK-…`. Without typing anything in the chat, a
   notification appears: the cancellation, same-day replacements within the original budget and
   time window (never the same provider), and a fresh card. Before approving, arm a clean outcome
   (`python3 -m errand_agent.cli demo next-booking success`): unarmed bookings follow the PDF's
   seeded 70/15/7.5/7.5 draw, so a `slot_taken` can come up. Reply `yes` (the client switches to the
   follow-up conversation) to book the replacement, or show that nothing happens without approval.
6. **Memory (optional, ≈30 s).** `what do you remember about me?`, then `forget memory 1`.

## After recording

Export the session's trace if you want it alongside the video:
`scripts/dev.sh python -m errand_agent.trace_export --db errand_demo --session N --out artifacts/traces/demo.jsonl`.
