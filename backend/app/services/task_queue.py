"""Per-robot priority task queue.

One device can only execute one ReAct agent run at a time (we share a single
WS request/response channel and a single UI). Earlier we just guarded each
spawn with an asyncio.Lock — that worked for correctness but meant:

  - no visibility into "what's waiting"
  - no priority distinction (a human-typed manual goal got the same slot as
    a chatbot auto-reply that arrived 200ms earlier)
  - in-flight task on a stuck device would block everything indefinitely

This module replaces that ad-hoc lock with a real per-robot queue:

  - `asyncio.PriorityQueue` ordered by (priority, sequence).
  - One consumer coroutine per robot, started lazily on first enqueue.
  - Each enqueue logs an audit row via `append_task_log` so the operator can
    see "waiting in queue" → "starting" → result.
  - Restartable: at app startup we re-queue any RobotTask rows left in
    `dispatched` / `queued` status (see `recover_pending_tasks`).

Constants like `PRIORITY_OPERATOR` are the public API for producers; numeric
values are internal — *lower number runs first*.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import Robot, RobotTask

log = logging.getLogger(__name__)


# ---- public priority constants -------------------------------------------
#
# Lower number = higher priority. Leave gaps for future tiers.
PRIORITY_OPERATOR = 0      # human-typed goal from /devices/[id] → /agent/run
PRIORITY_AUTO_REPLY = 50   # AI auto-reply (`send_text` from conv_agent)
PRIORITY_SCHEDULED = 100   # future: scanner-driven flows
PRIORITY_BACKGROUND = 200  # housekeeping / retry


# ---- types ---------------------------------------------------------------
# `Runner` signature: pass task_id → coroutine that drives the device end-
# to-end. Runners must look up the Robot themselves (via RobotTask.robot_id);
# the queue passes only the task pk.
Runner = Callable[[int], Awaitable[None]]


@dataclass(order=True)
class _Item:
    priority: int
    seq: int
    # exclude non-comparable fields from the ordering tuple
    task_id: int = field(compare=False)
    kind: str = field(compare=False)
    enqueued_at: float = field(compare=False)


# ---- the queue -----------------------------------------------------------
class RobotQueue:
    def __init__(self, robot_id: str) -> None:
        self.robot_id = robot_id
        self._q: asyncio.PriorityQueue[_Item] = asyncio.PriorityQueue()
        self._seq = 0  # monotonic tie-breaker — preserves FIFO within priority
        self._consumer: asyncio.Task[None] | None = None
        self._current: _Item | None = None
        self._registry: dict[str, Runner] = {}

    def register(self, kind: str, runner: Runner) -> None:
        self._registry[kind] = runner

    async def enqueue(self, kind: str, task_id: int, *, priority: int) -> None:
        self._seq += 1
        item = _Item(
            priority=priority,
            seq=self._seq,
            task_id=task_id,
            kind=kind,
            enqueued_at=time.monotonic(),
        )
        await self._q.put(item)
        log.info(
            "queue robot=%s enqueued kind=%s task=%s priority=%d depth=%d",
            self.robot_id, kind, task_id, priority, self._q.qsize(),
        )
        await _audit(task_id, "info", f"已入队 priority={priority} depth={self._q.qsize()}")
        # Lazy-start the consumer on first push.
        if self._consumer is None or self._consumer.done():
            self._consumer = asyncio.create_task(
                self._run(), name=f"robot-queue-{self.robot_id}"
            )

    def snapshot(self) -> dict:
        """For introspection / `/queue` endpoint."""
        # PriorityQueue exposes _queue (a heap list) — we copy it to peek
        # without consuming. Items are heap-ordered so we sort by priority
        # for readable output.
        pending = sorted(list(self._q._queue), key=lambda x: (x.priority, x.seq))  # type: ignore[attr-defined]
        return {
            "robot_id": self.robot_id,
            "running": (
                {
                    "kind": self._current.kind,
                    "task_id": self._current.task_id,
                    "priority": self._current.priority,
                    "waited_ms": int((time.monotonic() - self._current.enqueued_at) * 1000),
                }
                if self._current is not None
                else None
            ),
            "depth": len(pending),
            "pending": [
                {
                    "kind": p.kind,
                    "task_id": p.task_id,
                    "priority": p.priority,
                    "waited_ms": int((time.monotonic() - p.enqueued_at) * 1000),
                }
                for p in pending[:20]  # cap displayed
            ],
        }

    async def stop(self) -> None:
        if self._consumer is not None and not self._consumer.done():
            self._consumer.cancel()
            try:
                await self._consumer
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        log.info("queue consumer started robot=%s", self.robot_id)
        try:
            while True:
                item = await self._q.get()
                self._current = item
                runner = self._registry.get(item.kind)
                wait_ms = int((time.monotonic() - item.enqueued_at) * 1000)
                log.info(
                    "queue robot=%s start kind=%s task=%s priority=%d waited=%dms",
                    self.robot_id, item.kind, item.task_id, item.priority, wait_ms,
                )
                await _audit(
                    item.task_id, "info",
                    f"开始执行 kind={item.kind} 等待={wait_ms}ms",
                )
                if runner is None:
                    log.warning("queue robot=%s no runner for kind=%s", self.robot_id, item.kind)
                    await _audit(item.task_id, "warn", f"未知 kind={item.kind}，跳过")
                else:
                    try:
                        await runner(item.task_id)
                    except Exception as e:  # noqa: BLE001
                        log.exception(
                            "queue robot=%s runner crashed kind=%s task=%s",
                            self.robot_id, item.kind, item.task_id,
                        )
                        await _audit(item.task_id, "error", f"执行崩溃：{e}")
                self._current = None
                self._q.task_done()
        except asyncio.CancelledError:
            log.info("queue consumer cancelled robot=%s", self.robot_id)
            raise


# ---- module-level registry ------------------------------------------------
_QUEUES: dict[str, RobotQueue] = {}
_REGISTRY: dict[str, Runner] = {}


def register_runner(kind: str, runner: Runner) -> None:
    """Producer-side registration. Idempotent — call multiple times to update."""
    _REGISTRY[kind] = runner
    for q in _QUEUES.values():
        q.register(kind, runner)


def get_queue(robot_id: str) -> RobotQueue:
    q = _QUEUES.get(robot_id)
    if q is None:
        q = RobotQueue(robot_id)
        for k, r in _REGISTRY.items():
            q.register(k, r)
        _QUEUES[robot_id] = q
    return q


async def enqueue(
    robot_id: str, kind: str, task_id: int, *, priority: int
) -> None:
    await get_queue(robot_id).enqueue(kind, task_id, priority=priority)


def snapshot_all() -> list[dict]:
    return [q.snapshot() for q in _QUEUES.values()]


def snapshot(robot_id: str) -> dict:
    q = _QUEUES.get(robot_id)
    return q.snapshot() if q is not None else {
        "robot_id": robot_id, "running": None, "depth": 0, "pending": [],
    }


async def shutdown() -> None:
    """Cancel every consumer — call from FastAPI lifespan on shutdown."""
    for q in list(_QUEUES.values()):
        await q.stop()
    _QUEUES.clear()


# ---- boot-time recovery --------------------------------------------------
async def recover_pending_tasks() -> int:
    """Re-enqueue tasks left in `dispatched` status from a previous run.

    Best-effort: a task could have already partially completed before the
    crash, but the runner is idempotent enough (open_wecom + tap + input +
    send) that re-running it usually just sends the message again. For
    high-assurance flows the runner should check Message.status before
    starting (TODO)."""
    enqueued = 0
    async with SessionLocal() as db:
        # Resolve robot.id → robot.robot_id once.
        rows = (await db.execute(
            select(RobotTask, Robot.robot_id)
            .join(Robot, Robot.id == RobotTask.robot_id)
            .where(RobotTask.status == "dispatched")
        )).all()
        for task, rid in rows:
            kind = "send_text" if task.type == "send_text" else "agent_goal"
            priority = PRIORITY_AUTO_REPLY if kind == "send_text" else PRIORITY_OPERATOR
            await enqueue(rid, kind, task.id, priority=priority)
            enqueued += 1
    if enqueued:
        log.info("queue recovery: re-enqueued %d dispatched task(s)", enqueued)
    return enqueued


# ---- internals -----------------------------------------------------------
async def _audit(task_id: int, level: str, message: str) -> None:
    # Late import to avoid circular module load (send_orchestrator imports
    # this module for `enqueue`).
    from app.services.send_orchestrator import append_task_log

    try:
        async with SessionLocal() as db:
            robot = (await db.execute(
                select(Robot).join(RobotTask, RobotTask.robot_id == Robot.id)
                .where(RobotTask.id == task_id)
            )).scalars().first()
            if robot is None:
                return
            await append_task_log(
                db, robot=robot, task_id=task_id, level=level,
                message=f"[queue] {message}",
            )
    except Exception:  # noqa: BLE001
        log.exception("queue audit failed task=%s", task_id)


