from __future__ import annotations

import asyncio

import pytest

from app.services import auto_reply_scheduler


@pytest.mark.asyncio
async def test_wake_robot_rechecks_pending_work_while_idle(monkeypatch):
    auto_reply_scheduler._STATES.clear()
    calls = 0
    processed: list[int] = []

    async def fake_next_pending_conversation(db, robot_pk, state, *, exclude_ids=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            return type("Conv", (), {"id": 42})()
        return None

    async def fake_process_conversation(db, conv):
        processed.append(conv.id)

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, model, pk):
            # The dispatcher hands the worker a conv_id; the worker re-fetches
            # in its own session. For this test the model class doesn't matter
            # — we just round-trip the id.
            return type("Conv", (), {"id": pk})()

    monkeypatch.setattr(auto_reply_scheduler, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(
        auto_reply_scheduler,
        "_next_pending_conversation",
        fake_next_pending_conversation,
    )
    monkeypatch.setattr(
        auto_reply_scheduler,
        "_process_conversation",
        fake_process_conversation,
    )

    auto_reply_scheduler.wake_robot(1)
    await asyncio.sleep(0.05)
    auto_reply_scheduler.wake_robot(1)
    await asyncio.sleep(0.1)

    await auto_reply_scheduler.shutdown()
    assert processed == [42]


def test_decision_feedback_ids_prefers_messages_seen_by_workflow():
    decision = type("Decision", (), {"feedback_message_ids": [10, 11, 11]})()

    assert auto_reply_scheduler._decision_feedback_ids(decision, [10]) == [10, 11]


def test_decision_feedback_ids_falls_back_to_initial_batch():
    decision = type("Decision", (), {"feedback_message_ids": []})()

    assert auto_reply_scheduler._decision_feedback_ids(decision, [10]) == [10]


def test_decision_feedback_ids_handles_legacy_decision_without_field():
    decision = type("Decision", (), {})()

    assert auto_reply_scheduler._decision_feedback_ids(decision, [12]) == [12]
