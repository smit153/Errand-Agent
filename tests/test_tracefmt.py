"""The live trace pane: readable lines a presenter can explain, including what context reached the
model each turn (PDF §3 context management) and how a timeout after success was resolved."""

from __future__ import annotations

from datetime import UTC, datetime

from errand_agent.api.tracefmt import line, summary

AT = datetime(2026, 11, 8, 7, 39, 53, tzinfo=UTC)  # 13:09:53 in Asia/Kolkata


def test_model_call_shows_purpose_verdict_and_the_context_sent() -> None:
    payload = {
        "purpose": "act",
        "attempt": 1,
        "accepted": "select_option",
        "latency_ms": 4780.3,
        "usage": {"prompt_tokens": 4942, "completion_tokens": 22},
        "context": {
            "cap_bytes": 4096,
            "request_bytes": 1717,
            "plan": {
                "sections": {
                    "memories": {"offered": 6, "included": ["#14", "#15", "#11"]},
                    "reviews": {"offered": 3, "included": ["review-1", "review-2", "review-3"]},
                }
            },
        },
    }
    tag, text = line(1, AT, 7, "model.call", payload)
    assert tag == "model"
    first, second = text.split("\n")
    assert first.startswith("13:09:53  #7   MODEL")
    assert "act (pick next step) #1: proposed select_option ✓" in first
    assert "4.8 s" in first
    assert "4942 in / 22 out tokens" in first
    assert "sent 1717 of 4096 bytes; memories #14 #15 #11; 3 reviews as untrusted data" in second
    assert second.startswith(" " * first.index("act"))  # continuation lines up under the text


def test_rejected_model_call_is_an_error_line_with_the_reason() -> None:
    tag, _, text = summary(
        "model.call",
        {"purpose": "extract", "attempt": 1, "accepted": None, "problem": "intent is not stated", "context": {}},
    )
    assert tag == "error"
    assert "extract (read the message) #1: REJECTED, intent is not stated" in text


def test_timeout_after_success_reads_as_a_story() -> None:
    sent = summary(
        "tool.attempt_started",
        {"tool": "book", "args": {"hold_id": "HLD-000001", "idempotency_key": "op-1452f6fe178740ad99e4"}},
    )
    timed_out = summary(
        "tool.call",
        {"tool": "book", "error": {"code": "timeout", "effect": "unknown"}, "latency_ms": 26.2},
    )
    status = summary(
        "tool.call",
        {"tool": "get_booking_status", "result": {"status": "booked", "booking_id": "BK-000002"}, "latency_ms": 14.8},
    )
    done = summary(
        "operation.finished",
        {"status": "succeeded", "result_code": "booked", "verified": True, "booking_id": "BK-000002"},
    )
    assert sent == ("tool", "TOOL →", "book(hold HLD-000001, key op-1452f6fe1…)")
    assert timed_out == ("error", "TOOL ←", "book: ERROR timeout, effect unknown  (26 ms)")
    assert status[2] == "get_booking_status: booked BK-000002  (15 ms)"
    assert done[2] == "✔ succeeded: booked, verified BK-000002"


def test_request_changes_are_shown_as_values_not_json() -> None:
    _, label, text = summary(
        "agent.request_updated",
        {
            "changed": ["budget", "date"],
            "request": {"date": {"start": "2026-11-09", "end": "2026-11-15"}, "budget": {"max_inr": 1499}},
        },
    )
    assert label == "REQUEST"
    assert text == "changed: budget = up to ₹1499; date = 9 Nov–15 Nov"


def test_approval_and_memory_lines() -> None:
    assert summary("approval.decision", {"outcome": "duplicate", "form": "yes"})[2].startswith("already approved")
    assert summary("approval.decision", {"outcome": "rejected", "reason": "no_card_displayed"})[0] == "error"
    assert summary("memory.forgotten", {"display_no": 8})[2] == "forgotten #8"
    assert summary("memory.retrieved", {"query": "book plumber", "items": [{"display_no": 14}]})[2] == (
        'relevant to "book plumber": #14'
    )


def test_unknown_events_still_render() -> None:
    tag, label, text = summary("something.new", {"a": 1})
    assert (tag, label) == ("other", "something.new")
    assert '"a":1' in text


def test_run_lines_show_limits_and_usage() -> None:
    limits = {"active_seconds": 300, "tool_invocations": 20, "logical_model_calls": 10}
    assert summary("run.started", {"limits": limits})[2] == (
        "── run started · limits: 10 model calls, 20 tool calls, 300 s ──"
    )
    counters = {"logical_model_calls": 1, "tool_invocations": 2, "embedding_calls": 1}
    ended = summary("run.ended", {"exit_reason": "job:provider_cancellation", "counters": counters})[2]
    assert ended == (
        "ended (background job provider_cancellation, no user message): "
        "used 1 model call, 2 tool calls, 1 embedding; cost n/a (subscription)"
    )
