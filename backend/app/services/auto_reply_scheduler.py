"""Fair auto-reply scheduler.

Inbound customer messages are persisted as `feedback_status=pending`. This
scheduler walks pending conversations per robot, one conversation at a time,
so a noisy customer cannot monopolise the device. Each conversation turn may
enqueue at most two outbound reply tasks; then the scheduler rotates back to
the messages list and gives other pending customers a chance.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import func, select

from app.ai import workflow as ai_workflow
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.ws_manager import hub
from app.memory import summarizer
from app.models import Contact, Conversation, Message, Robot, RobotTask, utcnow
from app.schemas import ConversationOut, MessageOut
from app.services.send_orchestrator import create_and_dispatch_send_text

log = logging.getLogger(__name__)

MAX_REPLY_TASKS_PER_CONVERSATION_TURN = 2


@dataclass
class _RobotState:
    task: asyncio.Task[None] | None = None
    last_conv_id: int | None = None
    same_conv_turns: int = 0
    wake_count: int = 0
    wake_event: asyncio.Event | None = None


_STATES: dict[int, _RobotState] = {}


def wake_robot(robot_pk: int) -> None:
    if not settings.auto_reply_enabled or not settings.task_queue_enabled:
        log.info(
            "auto-reply wake ignored robot_pk=%s auto_reply_enabled=%s task_queue_enabled=%s",
            robot_pk,
            settings.auto_reply_enabled,
            settings.task_queue_enabled,
        )
        return
    state = _STATES.setdefault(robot_pk, _RobotState())
    state.wake_count += 1
    if state.wake_event is not None:
        state.wake_event.set()
    if state.task is None or state.task.done():
        state.task = asyncio.create_task(_run_robot(robot_pk), name=f"auto-reply-{robot_pk}")


async def recover_pending() -> None:
    if not settings.auto_reply_enabled or not settings.task_queue_enabled:
        log.info("auto-reply recovery skipped: auto reply or task queue disabled")
        return
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Conversation.robot_id)
                .join(Message, Message.conversation_id == Conversation.id)
                .where(
                    Message.direction == "in",
                    Message.sender_type == "customer",
                    Message.feedback_status.in_(("pending", "processing")),
                )
                .distinct()
            )
        ).scalars().all()
    for robot_pk in rows:
        wake_robot(int(robot_pk))


async def shutdown() -> None:
    for state in list(_STATES.values()):
        if state.task is not None and not state.task.done():
            state.task.cancel()
            try:
                await state.task
            except (asyncio.CancelledError, Exception):
                pass
    _STATES.clear()


async def _run_robot(robot_pk: int) -> None:
    state = _STATES.setdefault(robot_pk, _RobotState())
    seen_wake = state.wake_count
    while True:
        processed = False
        async with SessionLocal() as db:
            conv = await _next_pending_conversation(db, robot_pk, state)
            if conv is None:
                pass
            else:
                processed = True
                await _process_conversation(db, conv)
                if conv.id == state.last_conv_id:
                    state.same_conv_turns += 1
                else:
                    state.last_conv_id = conv.id
                    state.same_conv_turns = 1
        if processed:
            await asyncio.sleep(0.2)
            continue

        if state.wake_count != seen_wake:
            seen_wake = state.wake_count
            continue

        if state.wake_event is None:
            state.wake_event = asyncio.Event()
        state.wake_event.clear()
        if state.wake_count != seen_wake:
            seen_wake = state.wake_count
            continue
        try:
            await asyncio.wait_for(state.wake_event.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            break
        seen_wake = state.wake_count


async def _next_pending_conversation(db, robot_pk: int, state: _RobotState) -> Conversation | None:
    active_reply_exists = (
        select(RobotTask.id)
        .where(
            RobotTask.conversation_id == Conversation.id,
            RobotTask.type == "send_text",
            RobotTask.status.in_(("dispatched", "queued", "running")),
        )
        .exists()
    )
    candidate_ids = (
        await db.execute(
            select(
                Conversation.id,
                func.min(Message.created_at).label("oldest_pending_at"),
            )
            .join(Message, Message.conversation_id == Conversation.id)
            .where(
                Conversation.robot_id == robot_pk,
                Conversation.mode.in_(("ai", "mixed")),
                Message.direction == "in",
                Message.sender_type == "customer",
                Message.feedback_status.in_(("pending", "processing")),
                ~active_reply_exists,
            )
            .group_by(Conversation.id)
            .order_by(func.min(Message.created_at).asc())
        )
    ).all()
    if not candidate_ids:
        return None

    ordered_ids = [int(row.id) for row in candidate_ids]
    if state.last_conv_id is not None and state.same_conv_turns >= 2 and len(ordered_ids) > 1:
        for conv_id in ordered_ids:
            if conv_id != state.last_conv_id:
                return await db.get(Conversation, conv_id)
    return await db.get(Conversation, ordered_ids[0])


async def _process_conversation(db, conv: Conversation) -> None:
    if not settings.auto_reply_enabled or not settings.task_queue_enabled:
        log.info(
            "auto-reply processing skipped conv=%s auto_reply_enabled=%s task_queue_enabled=%s",
            conv.id,
            settings.auto_reply_enabled,
            settings.task_queue_enabled,
        )
        return
    robot = await db.get(Robot, conv.robot_id)
    contact = await db.get(Contact, conv.contact_id)
    if robot is None or contact is None:
        return
    pending_messages = await _pending_messages(db, conv.id)
    if not pending_messages:
        return
    batch_ids = [m.id for m in pending_messages]
    for msg in pending_messages:
        msg.feedback_status = "processing"
    await db.commit()

    try:
        decision = await ai_workflow.handle_inbound(
            db, robot=robot, conv=conv, message=pending_messages[-1]
        )
    except Exception:
        log.exception("AI workflow failed conv=%s", conv.id)
        await _mark_feedback(db, batch_ids, "failed")
        return

    await ai_workflow.broadcast_kb_hits(robot.team_id, conv.id, decision)
    if decision.action == "reply":
        reply_texts = decision.all_texts[:MAX_REPLY_TASKS_PER_CONVERSATION_TURN]
        if not reply_texts:
            await _mark_feedback(db, batch_ids, "failed")
            return
        task_ids: list[int] = []
        for text in reply_texts:
            _, task = await create_and_dispatch_send_text(
                db,
                robot=robot,
                conv=conv,
                contact_external_id=contact.external_id,
                text=text,
                sender_type="ai",
                sender_id=None,
                feedback_message_ids=batch_ids,
            )
            task_ids.append(task.id)
            if len(reply_texts) > 1:
                await asyncio.sleep(0.2)
        await _mark_feedback(db, batch_ids, "queued", decision.trace_id, task_ids)
    elif decision.action == "suggest":
        await ai_workflow.broadcast_suggestion(robot.team_id, conv.id, decision)
        await _mark_feedback(db, batch_ids, "suggested", decision.trace_id, [])
    else:
        await _mark_feedback(db, batch_ids, "skipped", decision.trace_id, [])
    if settings.memory_refresh_enabled:
        try:
            await summarizer.maybe_refresh(db, contact=contact, conv=conv)
        except Exception:
            log.exception("memory refresh failed")
    else:
        log.info("[message-callback] memory refresh disabled conv=%s", conv.id)


async def _pending_messages(db, conv_id: int) -> list[Message]:
    rows = (
        await db.execute(
            select(Message)
            .where(
                Message.conversation_id == conv_id,
                Message.direction == "in",
                Message.sender_type == "customer",
                Message.feedback_status.in_(("pending", "processing")),
            )
            .order_by(Message.created_at.asc())
            .limit(20)
        )
    ).scalars().all()
    return list(rows)


async def _mark_feedback(
    db,
    message_ids: list[int],
    status: str,
    trace_id: str | None = None,
    task_ids: list[int] | None = None,
) -> None:
    if not message_ids:
        return
    rows = (
        await db.execute(select(Message).where(Message.id.in_(message_ids)))
    ).scalars().all()
    now = utcnow()
    changed_conversations: set[int] = set()
    for msg in rows:
        msg.feedback_status = status
        msg.feedback_trace_id = trace_id
        msg.feedback_at = now
        msg.feedback_reply_task_ids = task_ids or []
        changed_conversations.add(msg.conversation_id)
    await db.commit()
    for msg in rows:
        await db.refresh(msg)
        await _broadcast_message_feedback(db, msg)
    for conv_id in changed_conversations:
        conv = await db.get(Conversation, conv_id)
        if conv is not None:
            await db.refresh(conv, attribute_names=["contact"])
            await _broadcast_conversation(conv)


async def _broadcast_message_feedback(db, msg: Message) -> None:
    conv = await db.get(Conversation, msg.conversation_id)
    if conv is None:
        return
    await hub.broadcast_web(
        conv.team_id,
        "message.updated",
        {
            "conversation_id": conv.id,
            "message": MessageOut.model_validate(msg).model_dump(mode="json"),
        },
    )


async def _broadcast_conversation(conv: Conversation) -> None:
    await hub.broadcast_web(
        conv.team_id,
        "conversation.updated",
        ConversationOut.model_validate(conv).model_dump(mode="json"),
    )
