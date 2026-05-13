"""Conversation + message-gateway service.

Handles the inbound side (Android → backend) and broadcasts to Web hub.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import workflow as ai_workflow
from app.core.ws_manager import hub
from app.memory import summarizer
from app.models import Contact, Conversation, Message, Robot, utcnow
from app.schemas import (
    AndroidMessageReceived,
    ContactOut,
    ConversationOut,
    MessageOut,
)
from app.services.task_dispatcher import create_and_dispatch_send_text


async def ingest_inbound_message(
    db: AsyncSession, robot: Robot, evt: AndroidMessageReceived
) -> Message | None:
    """Persist an inbound message; returns the Message or None if duplicate."""
    # dedupe by external_msg_id when provided
    if evt.external_msg_id:
        existing = (
            await db.execute(
                select(Message).where(Message.external_msg_id == evt.external_msg_id)
            )
        ).scalar_one_or_none()
        if existing:
            return None

    # upsert contact
    contact = (
        await db.execute(
            select(Contact).where(
                Contact.robot_id == robot.id,
                Contact.external_id == evt.contact.external_id,
            )
        )
    ).scalar_one_or_none()
    if contact is None:
        contact = Contact(
            team_id=robot.team_id,
            robot_id=robot.id,
            external_id=evt.contact.external_id,
            nickname=evt.contact.nickname,
            avatar=evt.contact.avatar,
        )
        db.add(contact)
        await db.flush()
    else:
        if evt.contact.nickname and evt.contact.nickname != contact.nickname:
            contact.nickname = evt.contact.nickname

    # find or create conversation
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.robot_id == robot.id, Conversation.contact_id == contact.id
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        conv = Conversation(
            team_id=robot.team_id, robot_id=robot.id, contact_id=contact.id
        )
        db.add(conv)
        await db.flush()

    msg = Message(
        conversation_id=conv.id,
        direction="in",
        sender_type="customer",
        type=evt.type,
        content=evt.content,
        external_msg_id=evt.external_msg_id,
        created_at=evt.sent_at or utcnow(),
    )
    db.add(msg)

    conv.unread_count = (conv.unread_count or 0) + 1
    conv.last_message_at = msg.created_at
    conv.last_message_preview = evt.content[:200]

    await db.commit()
    await db.refresh(msg)
    await db.refresh(conv)
    # eagerly load contact for serialization
    await db.refresh(conv, attribute_names=["contact"])

    # broadcast
    await hub.broadcast_web(
        robot.team_id,
        "message.new",
        {
            "conversation_id": conv.id,
            "message": MessageOut.model_validate(msg).model_dump(mode="json"),
        },
    )
    await hub.broadcast_web(
        robot.team_id,
        "conversation.updated",
        ConversationOut.model_validate(conv).model_dump(mode="json"),
    )

    # MVP2: trigger AI auto-reply when mode allows
    if conv.mode in ("ai", "mixed"):
        try:
            decision = await ai_workflow.handle_inbound(db, robot=robot, conv=conv, message=msg)
        except Exception:
            import logging
            logging.exception("AI workflow failed")
            decision = None

        if decision is not None:
            # always surface KB hits (right-panel "knowledge hits")
            await ai_workflow.broadcast_kb_hits(robot.team_id, conv.id, decision)

            if decision.action == "reply" and decision.text:
                await create_and_dispatch_send_text(
                    db,
                    robot=robot,
                    conv=conv,
                    contact_external_id=contact.external_id,
                    text=decision.text,
                    sender_type="ai",
                    sender_id=None,
                )
            elif decision.action == "suggest":
                await ai_workflow.broadcast_suggestion(robot.team_id, conv.id, decision)

    # MVP3: refresh long-term memory in the background of the request
    try:
        await summarizer.maybe_refresh(db, contact=contact, conv=conv)
    except Exception:
        import logging
        logging.exception("memory refresh failed")

    return msg
