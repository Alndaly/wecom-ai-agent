"""Task creation + dispatch to Android."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.ws_manager import hub
from app.models import Conversation, Message, Robot, RobotTask, RobotTaskLog
from app.schemas import MessageOut, TaskOut

log = logging.getLogger(__name__)


# Marker prepended to task.last_error after the ReAct agent has finished —
# prevents re-entry if the agent itself marks the task failed.
_REACT_PREFIX = "[react] "


async def create_and_dispatch_send_text(
    db: AsyncSession,
    *,
    robot: Robot,
    conv: Conversation,
    contact_external_id: str,
    text: str,
    sender_type: str,
    sender_id: int | None,
) -> tuple[Message, RobotTask]:
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
        payload_json={"conversation_external_id": contact_external_id, "text": text},
        status="pending",
        conversation_id=conv.id,
        message_id=msg.id,
    )
    db.add(task)
    await db.flush()
    db.add(RobotTaskLog(robot_id=robot.id, task_id=task.id, level="info", message=f"task created: send_text contact={contact_external_id}"))

    msg.task_id = task.id
    conv.last_message_at = msg.created_at
    conv.last_message_preview = text[:200]

    await db.commit()
    await db.refresh(msg)
    await db.refresh(task)

    # No more `task.dispatch` to Android for send_text — the backend ReAct
    # agent drives the device directly via `device.command` primitives. This
    # replaces the old hard-coded WeComAutomator heuristics entirely.
    task.status = "dispatched"
    db.add(RobotTaskLog(
        robot_id=robot.id, task_id=task.id,
        level="info", message="dispatched to ReAct agent",
    ))
    await db.commit()
    await db.refresh(task)

    asyncio.create_task(_run_send_via_react(robot_id=robot.id, task_id=task.id))
    await _broadcast_message_new(robot.team_id, conv.id, msg)
    return msg, task


async def update_task_on_callback(
    db: AsyncSession,
    *,
    robot: Robot,
    task_id: int,
    status: str,
    error: str | None = None,
) -> None:
    if task_id is None or task_id <= 0:
        return  # sentinel id from a local-test path — no row to update
    task = await db.get(RobotTask, task_id)
    if not task or task.robot_id != robot.id:
        return
    task.status = status
    if error:
        task.last_error = error
        db.add(RobotTaskLog(robot_id=robot.id, task_id=task.id, level="error", message=error))
    if task.message_id:
        msg = await db.get(Message, task.message_id)
        if msg:
            if status == "completed":
                msg.status = "sent"
            elif status in ("failed", "timeout"):
                msg.status = "failed"
    await db.commit()
    if task.message_id:
        msg = await db.get(Message, task.message_id)
        if msg:
            await _broadcast_message_update(robot.team_id, msg)
    await hub.broadcast_web(
        robot.team_id,
        "task.updated",
        {"task_id": task.id, "status": task.status, "error": task.last_error},
    )
    # Note: there used to be a "fallback to ReAct on failure" branch here.
    # ReAct is now the *primary* path for send_text (see
    # `_run_send_via_react`), so re-firing it on every task.failed callback
    # would create infinite loops. The callback path now only services
    # task types still dispatched the old way (none, for now).


async def append_task_log(
    db: AsyncSession,
    *,
    robot: Robot,
    task_id: int | None,
    level: str,
    message: str,
) -> None:
    # If a task_id is supplied but no row exists (e.g. -1 sentinel or stale ref
    # after a manual delete), Postgres' FK enforcement would reject the insert.
    # Drop the reference rather than crashing the whole inbound pipeline.
    safe_task_id: int | None = task_id
    if safe_task_id is not None:
        if safe_task_id <= 0:
            safe_task_id = None
        else:
            exists = await db.get(RobotTask, safe_task_id)
            if exists is None:
                safe_task_id = None
    db.add(RobotTaskLog(robot_id=robot.id, task_id=safe_task_id, level=level, message=message))
    await db.commit()
    await hub.broadcast_web(
        robot.team_id,
        "task.log",
        {
            "robot_id": robot.robot_id,
            "task_id": task_id,
            "level": level,
            "message": message,
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


def _redispatch_payload(task: RobotTask) -> dict[str, Any]:
    return {"task_id": task.id, "type": task.type, "payload": task.payload_json}


async def _run_send_via_react(*, robot_id: int, task_id: int) -> None:
    """Background coroutine: drive a send_text task end-to-end via the ReAct
    agent. ReAct is now the only execution path for send_text (no more
    hard-coded WeComAutomator heuristics).

    Best-effort opens WeCom before reasoning so the agent's first observation
    isn't of the launcher / a random app.

    Errors here must NOT raise into the caller — this is fire-and-forget.
    """
    # Late import to avoid circular dependency at module load time.
    from app.ai.react_agent import run_react

    async with SessionLocal() as db:
        try:
            task = await db.get(RobotTask, task_id)
            if task is None:
                return
            robot = await db.get(Robot, robot_id)
            if robot is None:
                return

            goal = _goal_for_task(task)
            if goal is None:
                return

            async def _sink(level: str, message: str) -> None:
                # Each step appended as its own log row + websocket broadcast.
                async with SessionLocal() as inner:
                    await append_task_log(
                        inner, robot=robot, task_id=task.id,
                        level=level if level in ("info", "warn", "error") else "info",
                        message=message,
                    )

            log.info("react send start task=%s goal=%r", task.id, goal)
            # Pre-flight: bring WeCom to foreground. Best-effort — if it
            # fails the agent will still observe whatever is on screen and
            # decide what to do.
            try:
                await asyncio.wait_for(
                    hub.send_request(
                        robot.robot_id, "device.command", {"command": "open_wecom"}
                    ),
                    timeout=6.0,
                )
                await asyncio.sleep(0.6)
            except Exception:  # noqa: BLE001
                log.debug("open_wecom pre-flight failed; continuing")

            result = await run_react(
                db, robot, goal,
                max_steps=settings.react_max_steps,
                step_timeout=settings.react_step_timeout_sec,
                log_sink=_sink,
            )
            task = await db.get(RobotTask, task_id)
            if task is None:
                return

            if result.ok:
                task.status = "completed"
                task.last_error = None
                if task.message_id:
                    msg = await db.get(Message, task.message_id)
                    if msg:
                        msg.status = "sent"
            else:
                task.status = "failed"
                task.last_error = _REACT_PREFIX + result.summary
            db.add(RobotTaskLog(
                robot_id=robot.id, task_id=task.id,
                level="info" if result.ok else "warn",
                message=f"[react] result ok={result.ok} steps={len(result.steps)} summary={result.summary}",
            ))
            await db.commit()
            await hub.broadcast_web(
                robot.team_id, "task.updated",
                {"task_id": task.id, "status": task.status, "error": task.last_error},
            )
            if task.message_id:
                m = await db.get(Message, task.message_id)
                if m:
                    await _broadcast_message_update(robot.team_id, m)
        except Exception as e:  # noqa: BLE001
            log.exception("react fallback crashed: %s", e)


def _goal_for_task(task: RobotTask) -> str | None:
    if task.type == "send_text":
        p = task.payload_json or {}
        contact = p.get("conversation_external_id") or "目标联系人"
        text = (p.get("text") or "").strip()
        if not text:
            return None
        # Truncate for prompt budget; the LLM doesn't need the full text to
        # decide *how* to navigate, only that it's a send-text intent.
        snippet = text if len(text) <= 80 else text[:80] + "…"
        return f"打开与「{contact}」的聊天，并发送下面这段文本：{snippet}"
    return None
