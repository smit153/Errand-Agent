"""Stage 3a invariants (E05–E09 approval side): approval is enforced by code, bound to one displayed revision.

Invariant under test throughout: **no operation exists without a valid approval of the exact,
currently displayed, unchanged revision**, and approval is consumed at most once.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from uuid import uuid4

import pytest

from errand_agent.clock import FakeClock
from errand_agent.db import Database
from errand_agent.mocks.backend import HOLD_TTL, MockBackend, ScenarioConfig
from errand_agent.mocks.catalog import DEMO_USER
from errand_agent.tools.client import ToolClient
from errand_agent.tools.context import ToolContext
from errand_agent.workflow.approval import ApprovalGate, ConfirmationResult, ProposalBlockedError
from errand_agent.workflow.snapshots import BookSnapshot, CancelSnapshot, RescheduleSnapshot
from tests.conftest import make_session
from tests.helpers import count
from tests.workflow_helpers import book_snapshot, cancel_snapshot, reschedule_snapshot


@pytest.fixture
def gate(db: Database, clock: FakeClock) -> ApprovalGate:
    return ApprovalGate(db, clock)


def _ops(db: Database) -> int:
    return count(db, "SELECT count(*) FROM operations")


def _approvals(db: Database) -> int:
    return count(db, "SELECT count(*) FROM approvals")


def _assert_nothing_approved(db: Database, result: ConfirmationResult) -> None:
    assert result.outcome in ("rejected", "not_confirmation")
    assert result.operation_id is None
    assert _ops(db) == 0
    assert _approvals(db) == 0


def _snapshot(
    action: str, tools: ToolClient, ctx: ToolContext, db: Database
) -> BookSnapshot | CancelSnapshot | RescheduleSnapshot:
    if action == "book":
        return book_snapshot(tools, ctx)
    if action == "cancel":
        return cancel_snapshot(db)
    return reschedule_snapshot(tools, ctx, db)


# ------------------------------------------------------------------ both forms, all three actions
@pytest.mark.invariant
@pytest.mark.parametrize("action", ["book", "cancel", "reschedule"])
@pytest.mark.parametrize("form", ["yes", "token"])
def test_both_confirmation_forms_approve_each_action(
    gate: ApprovalGate, tools: ToolClient, ctx: ToolContext, db: Database, action: str, form: str
) -> None:
    snap = _snapshot(action, tools, ctx, db)
    proposal = gate.propose(ctx.session_id, snap)
    receipt = gate.display(ctx.session_id, proposal.id)
    assert f"confirm {proposal.token}" in receipt.text
    text = "yes" if form == "yes" else f"confirm {proposal.token}"
    result = gate.confirm(ctx.session_id, DEMO_USER, text, receipt.id)
    assert result.outcome == "approved"
    assert result.operation_id is not None
    with db.txn() as conn:
        op = conn.execute("SELECT * FROM operations WHERE id = %s", (result.operation_id,)).fetchone()
        appr = conn.execute("SELECT form FROM approvals").fetchone()
        state = conn.execute("SELECT state FROM sessions WHERE id = %s", (ctx.session_id,)).fetchone()
    assert op is not None
    assert appr is not None
    assert state is not None
    assert op["action"] == action
    assert op["snapshot"] == snap.model_dump(mode="json")
    assert op["status"] == "approved"
    assert op["idempotency_key"]
    assert appr["form"] == form
    assert state["state"] == "executing"


@pytest.mark.parametrize("text", ["YES", " yes. ", "Yes!", "CONFIRM {token}", "confirm {token_lower}"])
def test_normalized_confirmation_variants_are_accepted(
    gate: ApprovalGate, tools: ToolClient, ctx: ToolContext, text: str
) -> None:
    proposal = gate.propose(ctx.session_id, book_snapshot(tools, ctx))
    receipt = gate.display(ctx.session_id, proposal.id)
    msg = text.format(token=proposal.token, token_lower=proposal.token.lower())
    assert gate.confirm(ctx.session_id, DEMO_USER, msg, receipt.id).outcome == "approved"


# ------------------------------------------------------------------ no confirmation / forged / absent card
@pytest.mark.invariant
@pytest.mark.parametrize(
    "text",
    ["sure", "ok go ahead", "yes, but make it 11am", "yes please book it", "yesterday", "confirm", "y", ""],
)
def test_anything_but_an_exact_confirmation_changes_nothing(
    gate: ApprovalGate, tools: ToolClient, ctx: ToolContext, db: Database, text: str
) -> None:
    proposal = gate.propose(ctx.session_id, book_snapshot(tools, ctx))
    receipt = gate.display(ctx.session_id, proposal.id)
    result = gate.confirm(ctx.session_id, DEMO_USER, text, receipt.id)
    assert result.outcome == "not_confirmation"
    _assert_nothing_approved(db, result)


@pytest.mark.invariant
def test_yes_without_a_displayed_card_is_rejected(
    gate: ApprovalGate, tools: ToolClient, ctx: ToolContext, db: Database
) -> None:
    gate.propose(ctx.session_id, book_snapshot(tools, ctx))  # created but never rendered
    result = gate.confirm(ctx.session_id, DEMO_USER, "yes", None)
    assert result.reason == "no_card_displayed"
    _assert_nothing_approved(db, result)


@pytest.mark.invariant
def test_forged_or_unknown_receipt_is_rejected(
    gate: ApprovalGate, tools: ToolClient, ctx: ToolContext, db: Database
) -> None:
    gate.propose(ctx.session_id, book_snapshot(tools, ctx))
    result = gate.confirm(ctx.session_id, DEMO_USER, "yes", uuid4())
    assert result.reason == "unknown_receipt"
    _assert_nothing_approved(db, result)


@pytest.mark.invariant
def test_wrong_token_is_rejected(gate: ApprovalGate, tools: ToolClient, ctx: ToolContext, db: Database) -> None:
    proposal = gate.propose(ctx.session_id, book_snapshot(tools, ctx))
    receipt = gate.display(ctx.session_id, proposal.id)
    result = gate.confirm(ctx.session_id, DEMO_USER, "confirm ZZZZZZ", receipt.id)
    assert result.reason == "token_mismatch"
    _assert_nothing_approved(db, result)


@pytest.mark.invariant
def test_receipt_or_token_cannot_be_reused_across_sessions(
    gate: ApprovalGate, tools: ToolClient, ctx: ToolContext, db: Database, clock: FakeClock
) -> None:
    proposal = gate.propose(ctx.session_id, book_snapshot(tools, ctx))
    receipt = gate.display(ctx.session_id, proposal.id)
    other_session = make_session(db, DEMO_USER, clock.now())
    for text in ("yes", f"confirm {proposal.token}"):
        result = gate.confirm(other_session, DEMO_USER, text, receipt.id)
        assert result.reason == "unknown_receipt"
        _assert_nothing_approved(db, result)


@pytest.mark.invariant
def test_another_user_cannot_confirm(gate: ApprovalGate, tools: ToolClient, ctx: ToolContext, db: Database) -> None:
    proposal = gate.propose(ctx.session_id, book_snapshot(tools, ctx))
    receipt = gate.display(ctx.session_id, proposal.id)
    result = gate.confirm(ctx.session_id, "user-intruder", "yes", receipt.id)
    assert result.reason == "not_owner"
    _assert_nothing_approved(db, result)


# ------------------------------------------------------------------ material changes (E07/E08)
@pytest.mark.invariant
def test_price_change_invalidates_the_old_yes_with_a_specific_explanation(
    gate: ApprovalGate, tools: ToolClient, ctx: ToolContext, db: Database, backend: MockBackend
) -> None:
    first = book_snapshot(tools, ctx)
    p1 = gate.propose(ctx.session_id, first)
    r1 = gate.display(ctx.session_id, p1.id)
    backend.configure(ScenarioConfig(quote_drift_inr={first.provider_id: 250}))
    requoted = book_snapshot(tools, ctx, nth_slot=0)  # re-hold: new hold, higher quote
    assert requoted.price_inr != first.price_inr
    p2 = gate.propose(ctx.session_id, requoted)
    # The user typed "yes" against the card they saw (r1); the newer card was never displayed.
    stale = gate.confirm(ctx.session_id, DEMO_USER, "yes", r1.id)
    assert stale.reason == "stale_revision"
    assert f"₹{first.price_inr:,}" in stale.message
    assert f"₹{requoted.price_inr:,}" in stale.message
    assert "no longer applies" in stale.message
    _assert_nothing_approved(db, stale)
    # The old token cannot approve the new card either.
    assert gate.confirm(ctx.session_id, DEMO_USER, f"confirm {p1.token}", r1.id).outcome == "rejected"
    # Fresh approval of the displayed new revision works and binds the new price.
    r2 = gate.display(ctx.session_id, p2.id)
    ok = gate.confirm(ctx.session_id, DEMO_USER, "yes", r2.id)
    assert ok.outcome == "approved"
    with db.txn() as conn:
        op = conn.execute("SELECT snapshot FROM operations WHERE id = %s", (ok.operation_id,)).fetchone()
    assert op is not None
    assert op["snapshot"]["price_inr"] == requoted.price_inr


@pytest.mark.invariant
@pytest.mark.parametrize("change", ["slot", "provider"])
def test_slot_or_provider_change_invalidates_approval(
    gate: ApprovalGate, tools: ToolClient, ctx: ToolContext, db: Database, change: str
) -> None:
    first = book_snapshot(tools, ctx)
    p1 = gate.propose(ctx.session_id, first)
    r1 = gate.display(ctx.session_id, p1.id)
    changed = book_snapshot(tools, ctx, nth_slot=1) if change == "slot" else book_snapshot(tools, ctx, provider_index=1)
    p2 = gate.propose(ctx.session_id, changed)
    gate.display(ctx.session_id, p2.id)
    for text in ("yes", f"confirm {p1.token}"):
        result = gate.confirm(ctx.session_id, DEMO_USER, text, r1.id)
        assert result.reason == "stale_revision"
        assert ("slot changed" if change == "slot" else "provider changed") in result.message
        _assert_nothing_approved(db, result)


@pytest.mark.invariant
def test_correction_while_typing_invalidates_pending_card(
    gate: ApprovalGate, tools: ToolClient, ctx: ToolContext, db: Database
) -> None:
    proposal = gate.propose(ctx.session_id, book_snapshot(tools, ctx))
    receipt = gate.display(ctx.session_id, proposal.id)
    gate.invalidate_pending(ctx.session_id, "user corrected the budget")
    result = gate.confirm(ctx.session_id, DEMO_USER, "yes", receipt.id)
    assert result.reason == "invalidated"
    _assert_nothing_approved(db, result)


# ------------------------------------------------------------------ hold expiry (E09)
@pytest.mark.invariant
def test_expired_hold_blocks_approval_with_template(
    gate: ApprovalGate, tools: ToolClient, ctx: ToolContext, db: Database, clock: FakeClock
) -> None:
    snap = book_snapshot(tools, ctx)
    proposal = gate.propose(ctx.session_id, snap)
    receipt = gate.display(ctx.session_id, proposal.id)
    clock.advance(HOLD_TTL)
    result = gate.confirm(ctx.session_id, DEMO_USER, "yes", receipt.id)
    assert result.reason == "hold_expired"
    assert "expired, so I haven't booked it" in result.message
    assert "needs your approval" in result.message
    _assert_nothing_approved(db, result)
    # The expired card stays dead even if the clock question is asked again.
    assert gate.confirm(ctx.session_id, DEMO_USER, "yes", receipt.id).outcome == "rejected"


def test_hold_with_one_second_left_is_still_approvable(
    gate: ApprovalGate, tools: ToolClient, ctx: ToolContext, clock: FakeClock
) -> None:
    proposal = gate.propose(ctx.session_id, book_snapshot(tools, ctx))
    receipt = gate.display(ctx.session_id, proposal.id)
    clock.advance(HOLD_TTL - timedelta(seconds=1))
    assert gate.confirm(ctx.session_id, DEMO_USER, "yes", receipt.id).outcome == "approved"


# ------------------------------------------------------------------ repeated confirmation (E06)
@pytest.mark.invariant
def test_repeated_confirmation_returns_the_existing_operation(
    gate: ApprovalGate, tools: ToolClient, ctx: ToolContext, db: Database, clock: FakeClock, test_conninfo: str
) -> None:
    proposal = gate.propose(ctx.session_id, book_snapshot(tools, ctx))
    receipt = gate.display(ctx.session_id, proposal.id)
    first = gate.confirm(ctx.session_id, DEMO_USER, "yes", receipt.id)
    again = gate.confirm(ctx.session_id, DEMO_USER, f"confirm {proposal.token}", receipt.id)
    # "Restart": a new process with a new pool and gate sees the same durable approval.
    restarted_db = Database(test_conninfo)
    try:
        after_restart = ApprovalGate(restarted_db, clock).confirm(ctx.session_id, DEMO_USER, "yes", receipt.id)
    finally:
        restarted_db.close()
    assert first.outcome == "approved"
    assert again.outcome == after_restart.outcome == "duplicate"
    assert first.operation_id == again.operation_id == after_restart.operation_id
    assert _ops(db) == 1
    assert _approvals(db) == 1


@pytest.mark.invariant
def test_concurrent_confirmations_create_exactly_one_operation(
    gate: ApprovalGate, tools: ToolClient, ctx: ToolContext, db: Database
) -> None:
    proposal = gate.propose(ctx.session_id, book_snapshot(tools, ctx))
    receipt = gate.display(ctx.session_id, proposal.id)
    results: list[ConfirmationResult] = []
    barrier = threading.Barrier(5)

    def worker() -> None:
        barrier.wait()
        results.append(gate.confirm(ctx.session_id, DEMO_USER, "yes", receipt.id))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(r.outcome for r in results) == ["approved"] + ["duplicate"] * 4
    assert len({r.operation_id for r in results}) == 1
    assert _ops(db) == 1


# ------------------------------------------------------------------ proposal lifecycle
def test_reproposing_an_identical_snapshot_keeps_the_same_revision(
    gate: ApprovalGate, tools: ToolClient, ctx: ToolContext
) -> None:
    snap = book_snapshot(tools, ctx)
    assert gate.propose(ctx.session_id, snap).id == gate.propose(ctx.session_id, snap).id


@pytest.mark.invariant
def test_new_proposal_is_blocked_while_an_operation_is_unresolved(
    gate: ApprovalGate, tools: ToolClient, ctx: ToolContext
) -> None:
    proposal = gate.propose(ctx.session_id, book_snapshot(tools, ctx))
    receipt = gate.display(ctx.session_id, proposal.id)
    assert gate.confirm(ctx.session_id, DEMO_USER, "yes", receipt.id).outcome == "approved"
    with pytest.raises(ProposalBlockedError):
        gate.propose(ctx.session_id, book_snapshot(tools, ctx, nth_slot=1))


def test_only_the_latest_pending_revision_can_be_displayed(
    gate: ApprovalGate, tools: ToolClient, ctx: ToolContext
) -> None:
    p1 = gate.propose(ctx.session_id, book_snapshot(tools, ctx))
    gate.propose(ctx.session_id, book_snapshot(tools, ctx, nth_slot=1))
    with pytest.raises(ProposalBlockedError):
        gate.display(ctx.session_id, p1.id)
