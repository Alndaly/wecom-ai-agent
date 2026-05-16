"""Fair auto-reply scheduler.

Inbound customer messages are persisted as `feedback_status=pending`. This
scheduler walks pending conversations per robot and generates AI replies.

Concurrency model
-----------------
- Across robots: fully parallel — one dispatcher coroutine per robot.
- Within a robot, across conversations: up to
  `settings.auto_reply_concurrency_per_robot` LLM phases run in parallel.
- Within a single conversation: strictly serial. Reply order must match
  question order, and the per-contact UserProfile/UserMemory writes must
  not race. The dispatcher achieves this by tracking `active_conv_ids` —
  a conversation is never picked twice while a worker is still on it.
- Device dispatch (`create_and_dispatch_send_text`) feeds the per-robot
  RobotTaskQueue, which remains serial — exactly one physical device.

The LLM phase and the device phase form a pipeline: while conv X waits on
the device queue, conv Y can already be in LLM generation.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

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
    workers: set[asyncio.Task[None]] = field(default_factory=set)
    active_conv_ids: set[int] = field(default_factory=set)
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
        # Drain any in-flight workers too.
        for w in list(state.workers):
            if not w.done():
                w.cancel()
        if state.workers:
            await asyncio.gather(*list(state.workers), return_exceptions=True)
    _STATES.clear()


async def _run_robot(robot_pk: int) -> None:
    """Per-robot dispatcher: launches up to N parallel workers, one per
    distinct pending conversation. The semaphore bounds LLM concurrency on
    this robot."""
    state = _STATES.setdefault(robot_pk, _RobotState())
    sem = asyncio.Semaphore(max(1, int(settings.auto_reply_concurrency_per_robot)))
    seen_wake = state.wake_count
    try:
        while True:
            async with SessionLocal() as db:
                conv = await _next_pending_conversation(
                    db, robot_pk, state, exclude_ids=state.active_conv_ids
                )
            if conv is not None:
                # Bound concurrency. acquire() may suspend if N workers are
                # already running — that's the desired backpressure.
                await sem.acquire()
                state.active_conv_ids.add(conv.id)
                worker = asyncio.create_task(
                    _run_worker(robot_pk, conv.id, state, sem),
                    name=f"auto-reply-{robot_pk}-conv-{conv.id}",
                )
                state.workers.add(worker)
                worker.add_done_callback(state.workers.discard)
                # Immediately try to dispatch the next conv — we don't sleep
                # between launches.
                continue

            # Nothing dispatchable right now. Either everything pending is
            # already being worked on, or there's nothing pending. Wait for
            # a wake signal or for a worker to finish (which may free a slot
            # or surface a newly-eligible conv).
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
                # Idle exit only when nothing is in flight either.
                if not state.workers:
                    break
            seen_wake = state.wake_count
    finally:
        # Don't leave orphan workers behind; let them finish their current
        # `_process_conversation` so DB state is consistent.
        if state.workers:
            await asyncio.gather(*list(state.workers), return_exceptions=True)


async def _run_worker(
    robot_pk: int, conv_id: int, state: _RobotState, sem: asyncio.Semaphore
) -> None:
    """Process one conversation's LLM phase with its own DB session."""
    try:
        async with SessionLocal() as db:
            conv = await db.get(Conversation, conv_id)
            if conv is not None:
                await _process_conversation(db, conv)
    except Exception:
        log.exception("auto-reply worker crashed robot=%s conv=%s", robot_pk, conv_id)
    finally:
        state.active_conv_ids.discard(conv_id)
        if conv_id == state.last_conv_id:
            state.same_conv_turns += 1
        else:
            state.last_conv_id = conv_id
            state.same_conv_turns = 1
        sem.release()
        # Nudge the dispatcher: a conv has freed up, and there may be
        # follow-up messages or other convs newly eligible.
        state.wake_count += 1
        if state.wake_event is not None:
            state.wake_event.set()


async def _next_pending_conversation(
    db,
    robot_pk: int,
    state: _RobotState,
    *,
    exclude_ids: set[int] | None = None,
) -> Conversation | None:
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

    excluded = exclude_ids or set()
    ordered_ids = [int(row.id) for row in candidate_ids if int(row.id) not in excluded]
    if not ordered_ids:
        return None
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
        summarizer.schedule_refresh(contact.id, conv.id)
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
