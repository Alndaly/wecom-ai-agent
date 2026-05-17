from __future__ import annotations

import asyncio

import pytest

from app.services import task_queue


@pytest.mark.asyncio
async def test_drain_robot_rejects_reentrant_same_robot():
    robot_id = "robot_busy"
    lock = task_queue._ROBOT_LOCKS.setdefault(robot_id, asyncio.Lock())
    await lock.acquire()
    try:
        assert await task_queue.drain_robot(robot_id) is False
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_drain_robot_runs_claimed_tasks_serially(monkeypatch):
    old_registry = dict(task_queue._REGISTRY)
    task_queue._REGISTRY.clear()
    task_queue._ROBOT_LOCKS.pop("robot_1", None)
    claimed = [(1, "send_text"), (2, "agent_goal"), None]
    executed: list[tuple[str, int]] = []

    async def fake_claim_next(robot_id):
        assert robot_id == "robot_1"
        return claimed.pop(0)

    async def send_text(task_id):
        executed.append(("send_text", task_id))

    async def agent_goal(task_id):
        executed.append(("agent_goal", task_id))

    monkeypatch.setattr(task_queue, "_claim_next", fake_claim_next)
    task_queue.register_runner("send_text", send_text)
    task_queue.register_runner("agent_goal", agent_goal)

    try:
        assert await task_queue.drain_robot("robot_1") is True
        assert executed == [("send_text", 1), ("agent_goal", 2)]
    finally:
        task_queue._REGISTRY.clear()
        task_queue._REGISTRY.update(old_registry)
