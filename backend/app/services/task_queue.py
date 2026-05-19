"""Celery-backed per-robot device task queue.

Celery is responsible for durable wakeups and cross-process scheduling. The
backend process still owns the Android WebSocket, so a Celery worker never
drives the phone directly; it asks the backend to drain one robot. The backend
then enforces the important invariant locally: one active device task per
robot, while different robots can drain in parallel.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from sqlalchemy import func, select

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import Message, Robot, RobotTask

log = logging.getLogger(__name__)


PRIORITY_OPERATOR = 0
PRIORITY_AUTO_REPLY = 50
PRIORITY_SCHEDULED = 100
PRIORITY_BACKGROUND = 200

RUNNABLE_STATUSES = ("dispatched", "queued")
ACTIVE_STATUSES = ("running",)
TERMINAL_STATUSES = ("completed", "failed", "cancelled", "timeout")

Runner = Callable[[int], Awaitable[None]]

_REGISTRY: dict[str, Runner] = {}
_ROBOT_LOCKS: dict[str, asyncio.Lock] = {}


def register_runner(kind: str, runner: Runner) -> None:
    _REGISTRY[kind] = runner


async def enqueue(robot_id: str, kind: str, task_id: int, *, priority: int) -> None:
    if not settings.task_queue_enabled:
        await _mark_cancelled(task_id, "任务队列已禁用，当前仅记录消息回调")
        log.info("queue disabled robot=%s kind=%s task=%s ignored", robot_id, kind, task_id)
        return

    async with SessionLocal() as db:
        task = await db.get(RobotTask, task_id)
        robot = (
            await db.execute(select(Robot).where(Robot.robot_id == robot_id))
        ).scalar_one_or_none()
        if task is None or robot is None or task.robot_id != robot.id:
            log.warning("queue enqueue rejected robot=%s kind=%s task=%s", robot_id, kind, task_id)
            return
        if task.status in TERMINAL_STATUSES:
            log.info("queue enqueue ignored terminal task=%s status=%s", task.id, task.status)
            return
        task.status = "queued"
        task.priority = int(priority)
        task.queue_seq = await _next_queue_seq(db, robot.id)
        task.last_error = None
        await db.commit()

    await _audit(task_id, "info", f"已入队 priority={priority}")
    _wake_celery(robot_id)


async def drain_robot(robot_id: str) -> bool:
    """Run pending tasks for one robot until the DB queue is empty.

    Returns False when the robot is already being drained in this backend
    process. The caller should retry later; this is the per-device serial guard.
    """
    lock = _ROBOT_LOCKS.setdefault(robot_id, asyncio.Lock())
    if lock.locked():
        return False

    await lock.acquire()
    try:
        while True:
            item = await _claim_next(robot_id)
            if item is None:
                return True
            task_id, kind = item
            runner = _REGISTRY.get(kind)
            if runner is None:
                await _audit(task_id, "warn", f"未知 kind={kind}，跳过")
                await _mark_failed(task_id, f"unknown task kind: {kind}")
                continue
            try:
                await runner(task_id)
            except asyncio.CancelledError:
                await _mark_cancelled(task_id, "任务执行已中断")
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("queue runner crashed robot=%s kind=%s task=%s", robot_id, kind, task_id)
                await _audit(task_id, "error", f"执行崩溃：{e}")
                await _mark_failed(task_id, f"runner crashed: {e}")
    finally:
        lock.release()


async def cancel(robot_id: str, task_id: int) -> bool:
    async with SessionLocal() as db:
        task = await db.get(RobotTask, task_id)
        robot = (
            await db.execute(select(Robot).where(Robot.robot_id == robot_id))
        ).scalar_one_or_none()
        if task is None or robot is None or task.robot_id != robot.id:
            return False
        if task.status in TERMINAL_STATUSES:
            return False
        if task.status in ACTIVE_STATUSES:
            # Celery cannot reliably interrupt the coroutine that is currently
            # driving an Android UI through the backend WS. Marking cancellation
            # as pending avoids lying to the operator.
            return False
    await _mark_cancelled(task_id, "任务已从等待队列中取消")
    return True


async def recover_pending_tasks() -> int:
    if not settings.task_queue_enabled:
        log.info("queue recovery skipped: task queue disabled")
        return 0
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(RobotTask, Robot.robot_id)
                .join(Robot, Robot.id == RobotTask.robot_id)
                .where(RobotTask.status.in_(("dispatched", "queued", "running")))
                .order_by(RobotTask.created_at.asc(), RobotTask.id.asc())
            )
        ).all()
        recovered = 0
        robot_ids_to_wake: set[str] = set()
        for task, rid in rows:
            if task.status == "running":
                task.status = "queued"
            if task.status == "dispatched":
                task.status = "queued"
            if task.priority is None:
                task.priority = _default_priority(task.type)
            if task.queue_seq is None:
                task.queue_seq = await _next_queue_seq(db, task.robot_id)
            recovered += 1
            robot_ids_to_wake.add(str(rid))
        await db.commit()
    for rid in robot_ids_to_wake:
        _wake_celery(rid)
    if recovered:
        log.info("queue recovery: re-woke %d pending task(s)", recovered)
    return recovered


async def shutdown() -> None:
    # Celery owns queued wakeups. In-process locks are released by coroutine
    # completion/cancellation during FastAPI shutdown.
    return None


def snapshot_all() -> list[dict]:
    raise RuntimeError("snapshot_all is async-only for the Celery queue")


async def snapshot(robot_id: str) -> dict:
    async with SessionLocal() as db:
        robot = (
            await db.execute(select(Robot).where(Robot.robot_id == robot_id))
        ).scalar_one_or_none()
        if robot is None:
            return {"robot_id": robot_id, "running": None, "depth": 0, "pending": []}
        running = (
            await db.execute(
                select(RobotTask)
                .where(RobotTask.robot_id == robot.id, RobotTask.status == "running")
                .order_by(RobotTask.updated_at.asc(), RobotTask.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        pending = (
            await db.execute(
                select(RobotTask)
                .where(RobotTask.robot_id == robot.id, RobotTask.status.in_(RUNNABLE_STATUSES))
                .order_by(RobotTask.priority.asc(), RobotTask.queue_seq.asc(), RobotTask.id.asc())
            )
        ).scalars().all()
        return {
            "robot_id": robot_id,
            "running": _task_snapshot(running) if running is not None else None,
            "depth": len(pending),
            "pending": [_task_snapshot(t) for t in pending],
        }


async def _claim_next(robot_id: str) -> tuple[int, str] | None:
    async with SessionLocal() as db:
        robot = (
            await db.execute(
                select(Robot).where(Robot.robot_id == robot_id).with_for_update()
            )
        ).scalar_one_or_none()
        if robot is None:
            return None
        running = (
            await db.execute(
                select(RobotTask.id)
                .where(RobotTask.robot_id == robot.id, RobotTask.status == "running")
                .limit(1)
            )
        ).scalar_one_or_none()
        if running is not None:
            return None
        task = (
            await db.execute(
                select(RobotTask)
                .where(RobotTask.robot_id == robot.id, RobotTask.status.in_(RUNNABLE_STATUSES))
                .order_by(RobotTask.priority.asc(), RobotTask.queue_seq.asc(), RobotTask.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if task is None:
            return None
        task.status = "running"
        task.last_error = None
        await db.commit()
        await _audit(task.id, "info", f"开始执行 kind={task.type}")
        await _broadcast_task(task.id)
        return task.id, task.type


async def _next_queue_seq(db, robot_pk: int) -> int:
    current = (
        await db.execute(
            select(func.max(RobotTask.queue_seq)).where(RobotTask.robot_id == robot_pk)
        )
    ).scalar_one_or_none()
    return int(current or 0) + 1


def _wake_celery(robot_id: str) -> None:
    try:
        from app.worker import celery_app

        celery_app.send_task(
            "app.worker.drain_robot_queue",
            args=[robot_id],
            queue="device_tasks",
            routing_key="device_tasks",
        )
    except Exception:  # noqa: BLE001
        log.exception("celery wake failed robot=%s", robot_id)


def _default_priority(kind: str) -> int:
    if kind == "agent_goal":
        return PRIORITY_OPERATOR
    if kind in {"send_text", "send_media"}:
        return PRIORITY_AUTO_REPLY
    return PRIORITY_BACKGROUND


def _task_snapshot(task: RobotTask) -> dict:
    created = task.created_at
    waited_ms = 0
    if created is not None:
        waited_ms = int((datetime.now(timezone.utc) - created).total_seconds() * 1000)
    title, detail = _task_labels_from_payload(task)
    return {
        "kind": task.type,
        "task_id": task.id,
        "title": title,
        "detail": detail,
        "priority": int(task.priority if task.priority is not None else _default_priority(task.type)),
        "waited_ms": max(waited_ms, 0),
        "cancellable": task.status != "running",
        "warning": task.last_error,
    }


def _task_labels_from_payload(task: RobotTask) -> tuple[str, str | None]:
    payload = task.payload_json or {}
    if task.type == "agent_goal":
        goal = str(payload.get("goal") or "").strip()
        return goal or f"语义指令 #{task.id}", None
    if task.type == "send_text":
        contact = str(payload.get("conversation_external_id") or "目标联系人").strip()
        text = str(payload.get("text") or "").strip()
        return f"发送给「{contact}」", text if text else None
    if task.type == "send_media":
        contact = str(payload.get("conversation_external_id") or "目标联系人").strip()
        media = payload.get("media") or {}
        filename = str(media.get("filename") or "").strip()
        label = "图片" if payload.get("kind") == "image" else "视频"
        return f"发送{label}给「{contact}」", filename or None
    return f"{task.type} #{task.id}", None


async def _audit(task_id: int, level: str, message: str) -> None:
    from app.services.send_orchestrator import append_task_log

    try:
        async with SessionLocal() as db:
            robot = (
                await db.execute(
                    select(Robot)
                    .join(RobotTask, RobotTask.robot_id == Robot.id)
                    .where(RobotTask.id == task_id)
                )
            ).scalars().first()
            if robot is not None:
                await append_task_log(
                    db,
                    robot=robot,
                    task_id=task_id,
                    level=level,
                    message=f"[queue] {message}",
                )
    except Exception:  # noqa: BLE001
        log.exception("queue audit failed task=%s", task_id)


async def _mark_cancelled(task_id: int, message: str) -> None:
    async with SessionLocal() as db:
        task = await db.get(RobotTask, task_id)
        if task is None:
            return
        robot = await db.get(Robot, task.robot_id)
        if robot is None:
            return
        task.status = "cancelled"
        task.last_error = message
        if task.message_id:
            msg = await db.get(Message, task.message_id)
            if msg is not None:
                msg.status = "cancelled"
        await db.commit()
    await _audit(task_id, "warn", message)
    await _broadcast_task(task_id)


async def _mark_failed(task_id: int, message: str) -> None:
    async with SessionLocal() as db:
        task = await db.get(RobotTask, task_id)
        if task is None:
            return
        task.status = "failed"
        task.last_error = message
        await db.commit()
    await _broadcast_task(task_id)


async def _broadcast_task(task_id: int) -> None:
    from app.core.ws_manager import hub
    from app.schemas import MessageOut

    async with SessionLocal() as db:
        task = await db.get(RobotTask, task_id)
        if task is None:
            return
        robot = await db.get(Robot, task.robot_id)
        if robot is None:
            return
        await hub.broadcast_web(
            robot.team_id,
            "task.updated",
            {"task_id": task.id, "status": task.status, "error": task.last_error},
        )
        if task.message_id and task.conversation_id:
            msg = await db.get(Message, task.message_id)
            if msg is not None:
                await hub.broadcast_web(
                    robot.team_id,
                    "message.updated",
                    {
                        "conversation_id": task.conversation_id,
                        "message": MessageOut.model_validate(msg).model_dump(mode="json"),
                    },
                )
