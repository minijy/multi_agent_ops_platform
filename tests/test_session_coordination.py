from __future__ import annotations

import threading
import time

import pytest

from ops_agent.runtime.session_coordination import SessionBusyError, SessionTurnCoordinator


def test_session_lock_isolated_by_tenant_user_and_session():
    coordinator = SessionTurnCoordinator(lock_wait_seconds=0.05)
    first = coordinator.acquire(tenant_id="tenant-a", user_id="alice", session_id="session-1")
    try:
        with pytest.raises(SessionBusyError):
            coordinator.acquire(tenant_id="tenant-a", user_id="alice", session_id="session-1")
        other_session = coordinator.acquire(
            tenant_id="tenant-a", user_id="alice", session_id="session-2"
        )
        other_user = coordinator.acquire(
            tenant_id="tenant-a", user_id="bob", session_id="session-1"
        )
        other_tenant = coordinator.acquire(
            tenant_id="tenant-b", user_id="alice", session_id="session-1"
        )
        other_session.release()
        other_user.release()
        other_tenant.release()
    finally:
        first.release()
        coordinator.close()


def test_same_session_waits_then_runs_in_order():
    coordinator = SessionTurnCoordinator(lock_wait_seconds=1)
    order: list[str] = []

    def first_turn():
        with coordinator.acquire(tenant_id="tenant-a", user_id="alice", session_id="session-1"):
            order.append("first-start")
            time.sleep(0.05)
            order.append("first-end")

    thread = threading.Thread(target=first_turn)
    thread.start()
    time.sleep(0.01)
    with coordinator.acquire(tenant_id="tenant-a", user_id="alice", session_id="session-1"):
        order.append("second")
    thread.join()
    coordinator.close()
    assert order == ["first-start", "first-end", "second"]


def test_idempotency_is_scoped_and_stable():
    coordinator = SessionTurnCoordinator(idempotency_ttl_seconds=60)
    session = coordinator.stable_session_id("tenant-a", "alice", "message-1")
    assert session == coordinator.stable_session_id("tenant-a", "alice", "message-1")
    assert session != coordinator.stable_session_id("tenant-a", "bob", "message-1")

    coordinator.put_result(
        tenant_id="tenant-a",
        user_id="alice",
        idempotency_key="message-1",
        payload={"answer": "ok"},
    )
    assert coordinator.get_result(
        tenant_id="tenant-a", user_id="alice", idempotency_key="message-1"
    ) == {"answer": "ok"}
    assert (
        coordinator.get_result(
            tenant_id="tenant-a", user_id="bob", idempotency_key="message-1"
        )
        is None
    )
    coordinator.close()
