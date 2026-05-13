"""Task creation + dispatch to Android."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ws_manager import hub
from app.models import Conversation, Message, Robot, RobotTask
from app.schemas import MessageOut, TaskOut


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

    msg.task_id = task.id
    conv.last_message_at = msg.created_at
    conv.last_message_preview = text[:200]

    await db.commit()
    await db.refresh(msg)
    await db.refresh(task)

    await _try_dispatch(robot, task)
    await _broadcast_message_new(robot.team_id, conv.id, msg)
    return msg, task


async def _try_dispatch(robot: Robot, task: RobotTask) -> None:
    delivered = await hub.send_android(
        robot.robot_id,
        "task.dispatch",
        {"task_id": task.id, "type": task.type, "payload": task.payload_json},
    )
    if delivered:
        task.status = "dispatched"


async def update_task_on_callback(
    db: AsyncSession,
    *,
    robot: Robot,
    task_id: int,
    status: str,
    error: str | None = None,
) -> None:
    task = await db.get(RobotTask, task_id)
    if not task or task.robot_id != robot.id:
        return
    task.status = status
    if error:
        task.last_error = error
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
