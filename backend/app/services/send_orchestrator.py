"""Outgoing text orchestration.

The `robot_tasks` row is now an audit record for a backend-driven ReAct send,
not a command dispatched to Android. Android only receives typed
`device.command` primitives.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.ws_manager import hub
from app.device import DeviceClient
from app.models import Conversation, Message, Robot, RobotTask, RobotTaskLog
from app.models import utcnow
from app.schemas import MessageOut
from app.services import settings_service

log = logging.getLogger(__name__)

_REACT_PREFIX = "[react] "
_MAX_AUTO_REPLY_SEND_ATTEMPTS = 2
_AUTO_REPLY_RETRY_DELAY_SEC = 5.0


# Per-device serialisation is the job of services.task_queue (one consumer
# per robot, priority-ordered). Producers don't need to hold any lock here.


async def create_and_dispatch_send_text(
    db: AsyncSession,
    *,
    robot: Robot,
    conv: Conversation,
    contact_external_id: str,
    text: str,
    sender_type: str,
    sender_id: int | None,
    feedback_message_ids: list[int] | None = None,
) -> tuple[Message, RobotTask]:
    if not settings.task_queue_enabled:
        raise RuntimeError("task queue disabled")
    msg = Message(
        conversation_id=conv.id,
        direction="out",
        sender_type=sender_type,
        sender_id=sender_id,
        type="text",
        content=text,
        status="pending",
    )
    db.add(msg)
    await db.flush()

    task = RobotTask(
        robot_id=robot.id,
        type="send_text",
        payload_json={
            "conversation_external_id": contact_external_id,
            "text": text,
            "feedback_message_ids": feedback_message_ids or [],
        },
        status="dispatched",
        conversation_id=conv.id,
        message_id=msg.id,
    )
    db.add(task)
    await db.flush()
    db.add(
        RobotTaskLog(
            robot_id=robot.id,
            task_id=task.id,
            level="info",
            message=f"send_text scheduled via ReAct contact={contact_external_id}",
        )
    )

    msg.task_id = task.id
    conv.last_message_at = msg.created_at
    conv.last_message_preview = text

    await db.commit()
    await db.refresh(msg)
    await db.refresh(task)

    # Hand off to the per-robot priority queue. Auto-replies sit at
    # PRIORITY_AUTO_REPLY — operator-typed agent goals jump ahead of them.
    from app.services import task_queue

    await task_queue.enqueue(
        robot.robot_id, "send_text", task.id, priority=task_queue.PRIORITY_AUTO_REPLY
    )
    await _broadcast_message_new(robot.team_id, conv.id, msg)
    return msg, task


async def append_task_log(
    db: AsyncSession,
    *,
    robot: Robot,
    task_id: int | None,
    level: str,
    message: str,
) -> None:
    safe_task_id: int | None = task_id
    if safe_task_id is not None:
        if safe_task_id <= 0:
            safe_task_id = None
        else:
            exists = await db.get(RobotTask, safe_task_id)
            if exists is None:
                safe_task_id = None
    row = RobotTaskLog(
        robot_id=robot.id, task_id=safe_task_id, level=level, message=message
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await hub.broadcast_web(
        robot.team_id,
        "task.log",
        {
            "id": row.id,
            "robot_id": robot.robot_id,
            "task_id": task_id,
            "level": level,
            "message": message,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        },
    )


async def _broadcast_message_new(team_id: int, conv_id: int, msg: Message) -> None:
    await hub.broadcast_web(
        team_id,
        "message.new",
        {
            "conversation_id": conv_id,
            "message": MessageOut.model_validate(msg).model_dump(mode="json"),
        },
    )


async def _broadcast_message_update(team_id: int, msg: Message) -> None:
    await hub.broadcast_web(
        team_id,
        "message.updated",
        {
            "conversation_id": msg.conversation_id,
            "message": MessageOut.model_validate(msg).model_dump(mode="json"),
        },
    )


async def run_send_task(task_id: int) -> None:
    """Queue runner — invoked by services.task_queue once the device slot
    becomes available. Serialisation is the queue's job; this function just
    drives one task end-to-end."""
    from app.ai.react_agent import run_react

    async with SessionLocal() as db:
        try:
            task = await db.get(RobotTask, task_id)
            if task is None:
                return
            robot = await db.get(Robot, task.robot_id)
            if robot is None:
                return

            goal = _goal_for_task(task)
            if goal is None:
                return

            async def _sink(level: str, message: str) -> None:
                async with SessionLocal() as inner:
                    await append_task_log(
                        inner,
                        robot=robot,
                        task_id=task.id,
                        level=level if level in ("info", "warn", "error") else "info",
                        message=message,
                    )

            log.info("react send start task=%s goal=%r", task.id, goal)
            device = DeviceClient(robot)
            session_started = False
            try:
                session_started = True
                ack = await device.react_session_start(timeout=4.0)
                if not ack.ok:
                    log.debug("react session start not acknowledged: %s", ack.message)
            except Exception:  # noqa: BLE001
                log.debug("react session start failed; continuing")
            try:
                await device.open_wecom(timeout=6.0)
                await asyncio.sleep(0.6)
            except Exception:  # noqa: BLE001
                log.debug("open_wecom pre-flight failed; continuing")

            ai_cfg = await settings_service.get(db, robot.team_id, "ai")
            force_llm = bool(
                ai_cfg.get("react_force_llm")
                if ai_cfg.get("react_force_llm") is not None
                else settings.react_force_llm
            )
            max_attempts = min(int(task.max_attempts or 1), _MAX_AUTO_REPLY_SEND_ATTEMPTS)
            result = None
            try:
                while True:
                    result = await run_react(
                        db,
                        robot,
                        goal,
                        max_steps=settings.react_max_steps,
                        step_timeout=settings.react_step_timeout_sec,
                        log_sink=_sink,
                        force_llm=force_llm,
                    )
                    if result.ok:
                        break
                    task = await db.get(RobotTask, task_id)
                    if task is None:
                        return
                    next_attempts = int(task.attempts or 0) + 1
                    if next_attempts >= max_attempts:
                        break
                    task.attempts = next_attempts
                    task.last_error = _REACT_PREFIX + result.summary
                    db.add(
                        RobotTaskLog(
                            robot_id=robot.id,
                            task_id=task.id,
                            level="warn",
                            message=f"{task.last_error}; 保持设备槽位并立即重试 {task.attempts}/{task.max_attempts}",
                        )
                    )
                    await db.commit()
                    await asyncio.sleep(_AUTO_REPLY_RETRY_DELAY_SEC)
                    try:
                        await device.open_wecom(timeout=6.0)
                        await asyncio.sleep(0.6)
                    except Exception:  # noqa: BLE001
                        log.debug("open_wecom retry pre-flight failed; continuing")
            finally:
                if session_started:
                    try:
                        await asyncio.shield(device.react_session_end(timeout=4.0))
                    except Exception:  # noqa: BLE001
                        log.debug("react session end failed")
            assert result is not None
            if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            task = await db.get(RobotTask, task_id)
            if task is None:
                return
            retry_task_id: int | None = None

            if result.ok:
                task.status = "completed"
                task.last_error = None
                await _mark_feedback_messages(db, task, "replied")
                if task.message_id:
                    msg = await db.get(Message, task.message_id)
                    if msg:
                        msg.status = "sent"
            else:
                retry_task_id = await _handle_send_failure(db, robot, task, _REACT_PREFIX + result.summary)
            db.add(
                RobotTaskLog(
                    robot_id=robot.id,
                    task_id=task.id,
                    level="info" if result.ok else "warn",
                    message=f"[react] result ok={result.ok} steps={len(result.steps)} summary={result.summary}",
                )
            )
            await db.commit()
            if retry_task_id is not None:
                from app.services import task_queue

                await asyncio.sleep(_AUTO_REPLY_RETRY_DELAY_SEC)
                await task_queue.enqueue(
                    robot.robot_id,
                    "send_text",
                    retry_task_id,
                    priority=task_queue.PRIORITY_BACKGROUND,
                )
            await hub.broadcast_web(
                robot.team_id,
                "task.updated",
                {"task_id": task.id, "status": task.status, "error": task.last_error},
            )
            if task.message_id:
                m = await db.get(Message, task.message_id)
                if m:
                    await _broadcast_message_update(robot.team_id, m)
            await _wake_auto_reply_if_pending(db, robot.id, task.conversation_id)
        except asyncio.CancelledError:
            task = await db.get(RobotTask, task_id)
            if task is not None:
                task.status = "cancelled"
                task.last_error = "任务执行已中断"
                await _mark_feedback_messages(db, task, "failed")
                if task.message_id:
                    msg = await db.get(Message, task.message_id)
                    if msg:
                        msg.status = "cancelled"
                db.add(
                    RobotTaskLog(
                        robot_id=robot.id,
                        task_id=task.id,
                        level="warn",
                        message="[react] cancelled by operator",
                    )
                )
                await db.commit()
                await hub.broadcast_web(
                    robot.team_id,
                    "task.updated",
                    {"task_id": task.id, "status": task.status, "error": task.last_error},
                )
                if task.message_id:
                    m = await db.get(Message, task.message_id)
                    if m:
                        await _broadcast_message_update(robot.team_id, m)
                await _wake_auto_reply_if_pending(db, robot.id, task.conversation_id)
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("react send crashed: %s", e)


def _goal_for_task(task: RobotTask) -> str | None:
    if task.type != "send_text":
        return None
    payload = task.payload_json or {}
    contact = payload.get("conversation_external_id") or "目标联系人"
    text = (payload.get("text") or "").strip()
    if not text:
        return None
    return f"打开与「{contact}」的聊天，并发送下面这段文本：{text}"


async def _mark_feedback_messages(db: AsyncSession, task: RobotTask, status: str) -> None:
    ids = (task.payload_json or {}).get("feedback_message_ids") or []
    if not ids:
        return
    rows = (
        await db.execute(
            select(Message).where(
                Message.id.in_(ids),
                Message.direction == "in",
                Message.sender_type == "customer",
            )
        )
    ).scalars().all()
    for msg in rows:
        if status == "failed" and msg.feedback_status == "replied":
            continue
        msg.feedback_status = status
        msg.feedback_at = utcnow()


async def _handle_send_failure(
    db: AsyncSession, robot: Robot, task: RobotTask, error: str
) -> int | None:
    task.attempts = int(task.attempts or 0) + 1
    task.last_error = error
    if task.attempts < min(int(task.max_attempts or 1), _MAX_AUTO_REPLY_SEND_ATTEMPTS):
        task.status = "dispatched"
        await _mark_feedback_messages(db, task, "pending")
        if task.message_id:
            msg = await db.get(Message, task.message_id)
            if msg:
                msg.status = "pending"
        db.add(
            RobotTaskLog(
                robot_id=robot.id,
                task_id=task.id,
                level="warn",
                message=f"{error}; 将重试 {task.attempts}/{task.max_attempts}",
            )
        )
        return task.id

    task.status = "failed"
    await _mark_feedback_messages(db, task, "failed")
    if task.message_id:
        msg = await db.get(Message, task.message_id)
        if msg:
            msg.status = "failed"
    return None


async def _wake_auto_reply_if_pending(
    db: AsyncSession, robot_pk: int, conv_id: int | None
) -> None:
    if not settings.auto_reply_enabled:
        return
    if conv_id is None:
        return
    row = (
        await db.execute(
            select(Message.id)
            .where(
                Message.conversation_id == conv_id,
                Message.direction == "in",
                Message.sender_type == "customer",
                Message.feedback_status.in_(("pending", "processing")),
            )
            .limit(1)
        )
    ).first()
    if row is None:
        return
    from app.services import auto_reply_scheduler

    auto_reply_scheduler.wake_robot(robot_pk)
