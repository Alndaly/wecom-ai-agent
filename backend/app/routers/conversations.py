from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete as sa_delete, desc, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.ws_manager import hub
from app.deps import current_user
from app.models import AIReplyLog, Contact, Conversation, Message, Robot, RobotTask, User
from app.schemas import (
    ConversationOut,
    ConversationPatch,
    MessageOut,
    MessageSendIn,
    MessageSendOut,
    TaskOut,
)
from app.services.send_orchestrator import create_and_dispatch_send_text

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    robot_id: int | None = None,
    unread_only: bool = False,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Conversation]:
    stmt = select(Conversation).where(Conversation.team_id == user.team_id)
    if robot_id is not None:
        stmt = stmt.where(Conversation.robot_id == robot_id)
    if unread_only:
        stmt = stmt.where(Conversation.unread_count > 0)
    stmt = stmt.order_by(desc(Conversation.last_message_at)).limit(200)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


async def _get_conv(db: AsyncSession, cid: int, team_id: int) -> Conversation:
    conv = await db.get(Conversation, cid)
    if not conv or conv.team_id != team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    return conv


@router.get("/{cid}", response_model=ConversationOut)
async def get_conversation(
    cid: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> Conversation:
    return await _get_conv(db, cid, user.team_id)


@router.patch("/{cid}", response_model=ConversationOut)
async def patch_conversation(
    cid: int,
    body: ConversationPatch,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Conversation:
    conv = await _get_conv(db, cid, user.team_id)
    conv.mode = body.mode
    await db.commit()
    await db.refresh(conv)
    return conv


@router.post("/{cid}/read", response_model=ConversationOut)
async def mark_read(
    cid: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Conversation:
    """Clear the unread badge. Called when the operator opens (focuses) a
    conversation. Idempotent; also broadcasts so other Web tabs reset too."""
    conv = await _get_conv(db, cid, user.team_id)
    if conv.unread_count:
        conv.unread_count = 0
        await db.commit()
        await db.refresh(conv)
        from app.core.ws_manager import hub  # local import to avoid cycle
        await hub.broadcast_web(
            user.team_id,
            "conversation.updated",
            ConversationOut.model_validate(conv).model_dump(mode="json"),
        )
    return conv


@router.get("/{cid}/messages", response_model=list[MessageOut])
async def list_messages(
    cid: int,
    before: datetime | None = None,
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Message]:
    await _get_conv(db, cid, user.team_id)  # auth check
    stmt = select(Message).where(Message.conversation_id == cid)
    if before:
        stmt = stmt.where(Message.created_at < before)
    stmt = stmt.order_by(desc(Message.created_at)).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return list(reversed(rows))


@router.delete("/{cid}/messages/{mid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_message(
    cid: int,
    mid: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hard-delete a single message. Detaches any FK references first."""
    await _get_conv(db, cid, user.team_id)
    msg = await db.get(Message, mid)
    if msg is None or msg.conversation_id != cid:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "message not found")

    # AIReplyLog.message_id → set NULL (preserve the log, drop the ref)
    await db.execute(
        sa_update(AIReplyLog).where(AIReplyLog.message_id == mid).values(message_id=None)
    )
    await db.delete(msg)
    await db.commit()
    await hub.broadcast_web(
        user.team_id,
        "message.deleted",
        {"conversation_id": cid, "message_id": mid},
    )


@router.delete("/{cid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    cid: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hard-delete a conversation and ALL its messages, tasks and AI logs."""
    conv = await _get_conv(db, cid, user.team_id)
    # 1. AIReplyLog ← message_id (FK) — wipe entire conv
    await db.execute(sa_delete(AIReplyLog).where(AIReplyLog.conversation_id == cid))
    # 2. Messages reference robot_tasks via FK; tasks reference conv via FK too.
    #    Order: clear Message.task_id (FK), delete RobotTask rows for this conv,
    #    delete messages, then the conversation row.
    await db.execute(
        sa_update(Message).where(Message.conversation_id == cid).values(task_id=None)
    )
    await db.execute(sa_delete(RobotTask).where(RobotTask.conversation_id == cid))
    await db.execute(sa_delete(Message).where(Message.conversation_id == cid))
    await db.delete(conv)
    await db.commit()
    await hub.broadcast_web(
        user.team_id,
        "conversation.deleted",
        {"conversation_id": cid},
    )


@router.post("/{cid}/messages", response_model=MessageSendOut)
async def send_message(
    cid: int,
    body: MessageSendIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageSendOut:
    conv = await _get_conv(db, cid, user.team_id)
    robot = await db.get(Robot, conv.robot_id)
    contact = await db.get(Contact, conv.contact_id)
    if not robot or not contact:
        raise HTTPException(status.HTTP_409_CONFLICT, "robot or contact missing")
    msg, task = await create_and_dispatch_send_text(
        db,
        robot=robot,
        conv=conv,
        contact_external_id=contact.external_id,
        text=body.content,
        sender_type="human",
        sender_id=user.id,
    )
    return MessageSendOut(
        message=MessageOut.model_validate(msg),
        task=TaskOut.model_validate(task),
    )
