"""The live trace pane shows what context reached the model each turn (PDF §3 context management)."""

from __future__ import annotations

from errand_agent.api.tracefmt import summary


def test_model_call_line_shows_the_context_sent() -> None:
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
    line = summary("model.call", payload)
    assert "1717/4096 B" in line
    assert "memories #14,#15,#11 (3/6)" in line
    assert "reviews 3/3 as untrusted data" in line


def test_model_call_line_without_optional_sections() -> None:
    line = summary("model.call", {"purpose": "extract", "attempt": 1, "accepted": "update_request", "context": {}})
    assert "extract #1 accepted update_request" in line
    assert "memories" not in line
