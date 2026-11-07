"""Stage 1 gate (E32 primitives): durable claims, generation CAS and fencing under concurrency."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from errand_agent.claims import ClaimManager
from errand_agent.clock import FakeClock
from errand_agent.db import Database
from errand_agent.errors import SessionBusyError, StaleClaimError
from tests.conftest import make_session


@pytest.mark.invariant
def test_concurrent_acquire_exactly_one_wins(db: Database, user_id: str, clock: FakeClock) -> None:
    sid = make_session(db, user_id, clock.now())
    claims = ClaimManager(db, clock, lease_seconds=60)
    results: list[str] = []
    barrier = threading.Barrier(6)

    def worker(name: str) -> None:
        barrier.wait()
        try:
            claims.acquire(sid, name)
            results.append("won")
        except SessionBusyError:
            results.append("busy")

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(results) == ["busy"] * 5 + ["won"]


@pytest.mark.invariant
def test_expired_lease_takeover_fences_old_holder(db: Database, user_id: str, clock: FakeClock) -> None:
    sid = make_session(db, user_id, clock.now())
    claims = ClaimManager(db, clock, lease_seconds=60)
    old = claims.acquire(sid, "worker-a")
    with pytest.raises(SessionBusyError):
        claims.acquire(sid, "worker-b")
    clock.advance(timedelta(seconds=61))
    new = claims.acquire(sid, "worker-b")
    assert new.generation == old.generation + 1
    with db.session_txn(sid) as conn:
        claims.verify(conn, new)
        with pytest.raises(StaleClaimError):
            claims.verify(conn, old)
    # A fenced holder's release must not free the new holder's claim.
    claims.release(old)
    with pytest.raises(SessionBusyError):
        claims.acquire(sid, "worker-c")


@pytest.mark.invariant
def test_expired_lease_is_stale_even_without_takeover(db: Database, user_id: str, clock: FakeClock) -> None:
    sid = make_session(db, user_id, clock.now())
    claims = ClaimManager(db, clock, lease_seconds=60)
    claim = claims.acquire(sid, "worker-a")
    clock.advance(timedelta(seconds=60))
    with db.session_txn(sid) as conn, pytest.raises(StaleClaimError):
        claims.verify(conn, claim)
    with pytest.raises(StaleClaimError):
        claims.renew(claim)


def test_release_then_reacquire_bumps_generation(db: Database, user_id: str, clock: FakeClock) -> None:
    sid = make_session(db, user_id, clock.now())
    claims = ClaimManager(db, clock, lease_seconds=60)
    first = claims.acquire(sid, "worker-a")
    claims.release(first)
    second = claims.acquire(sid, "worker-b")
    assert second.generation == first.generation + 1


def test_claims_on_different_sessions_are_independent(db: Database, user_id: str, clock: FakeClock) -> None:
    s1 = make_session(db, user_id, clock.now())
    s2 = make_session(db, user_id, clock.now())
    claims = ClaimManager(db, clock, lease_seconds=60)
    assert claims.acquire(s1, "worker-a").generation == 1
    assert claims.acquire(s2, "worker-b").generation == 1
