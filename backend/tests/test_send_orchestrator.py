from __future__ import annotations

import pytest

from app.models import Message, Robot, RobotTask
from app.services import send_orchestrator


@pytest.mark.asyncio
async def test_skip_if_message_already_sent_marks_task_completed(monkeypatch):
    robot = Robot(id=7, team_id=3, robot_id="robot_1", token_hash="hash")
    task = RobotTask(
        id=11,
        robot_id=robot.id,
        type="send_text",
        payload_json={"feedback_message_ids": [21]},
        status="running",
        message_id=12,
        conversation_id=13,
        last_error="stale",
    )
    sent = Message(
        id=12,
        conversation_id=13,
        direction="out",
        sender_type="ai",
        type="text",
        content="你好",
        status="sent",
    )
    feedback = Message(
        id=21,
        conversation_id=13,
        direction="in",
        sender_type="customer",
        type="text",
        content="在吗",
        feedback_status="queued",
    )
    updates: list[tuple[int, Message]] = []
    broadcasts: list[tuple[int, str, dict]] = []

    class FakeDb:
        async def get(self, model, pk):
            if model is Message and pk == sent.id:
                return sent
            if model is Message and pk == feedback.id:
                return feedback
            return None

        async def execute(self, stmt):
            return FakeResult([feedback])

        def add(self, obj):
            pass

        async def commit(self):
            pass

    class FakeResult:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return self.rows

    async def fake_broadcast_message_update(team_id, msg):
        updates.append((team_id, msg))

    async def fake_broadcast_web(team_id, event, payload):
        broadcasts.append((team_id, event, payload))

    monkeypatch.setattr(
        send_orchestrator,
        "_broadcast_message_update",
        fake_broadcast_message_update,
    )
    monkeypatch.setattr(send_orchestrator.hub, "broadcast_web", fake_broadcast_web)

    skipped = await send_orchestrator._skip_if_message_already_sent(FakeDb(), robot, task)

    assert skipped
    assert task.status == "completed"
    assert task.last_error is None
    assert feedback.feedback_status == "replied"
    assert updates == [(robot.team_id, sent)]
    assert broadcasts == [
        (
            robot.team_id,
            "task.updated",
            {"task_id": task.id, "status": "completed", "error": None},
        )
    ]


@pytest.mark.asyncio
async def test_skip_if_message_already_sent_ignores_unsent_message():
    task = RobotTask(id=11, robot_id=7, type="send_text", status="running", message_id=12)
    msg = Message(
        id=12,
        conversation_id=13,
        direction="out",
        sender_type="ai",
        type="text",
        content="你好",
        status="pending",
    )

    class FakeDb:
        async def get(self, model, pk):
            return msg

    skipped = await send_orchestrator._skip_if_message_already_sent(
        FakeDb(),
        Robot(id=7, team_id=3, robot_id="robot_1", token_hash="hash"),
        task,
    )

    assert not skipped
    assert task.status == "running"
