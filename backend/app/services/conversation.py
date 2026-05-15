"""Conversation + message-gateway service.

Handles the inbound side (Android → backend) and broadcasts to Web hub.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

# Per-conversation lock. Serialises the AI reply path so that bursts of
# inbound messages from the same customer don't kick off N concurrent agent
# runs (which would each see a different subset of the unreplied chain and
# duplicate replies). The first lock holder processes the whole chain; queued
# arrivals re-enter, see "no unreplied tail" and exit cheaply.
_CONV_LOCKS: dict[int, asyncio.Lock] = {}


def _lock_for(conv_id: int) -> asyncio.Lock:
    lock = _CONV_LOCKS.get(conv_id)
    if lock is None:
        lock = asyncio.Lock()
        _CONV_LOCKS[conv_id] = lock
    return lock


async def _has_been_replied_after(
    db: AsyncSession, conv_id: int, after: datetime
) -> bool:
    """True iff there's an outbound message in this conversation strictly after
    `after`. Used to skip processing an inbound that a previous batch already
    answered."""
    row = (
        await db.execute(
            select(Message.id)
            .where(
                Message.conversation_id == conv_id,
                Message.direction == "out",
                Message.created_at > after,
            )
            .limit(1)
        )
    ).first()
    return row is not None

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# WeCom aggregates >1 pending unread messages in a single chat into a single
# notification whose body is prefixed with "[N条]". The Android listener strips
# it, but we mirror that here as a defence-in-depth — other inbound channels
# (REST replay, third-party recorders) may forward the raw text.
_AGG_PREFIX_RX = re.compile(r"^\s*\[\s*\d+\s*条\s*]\s*")
_BOT_ECHO_RX = re.compile(r"^\s*收到您说的[「\"']")
_TIME_ONLY_RX = re.compile(r"^\s*(上午|下午)?\s*\d{1,2}:\d{2}\s*$")

# WeCom internal system messages — these are platform notifications (weekly
# summary, app announcements, password reset, etc.) that look like normal chat
# bubbles but are NOT customer questions. Replying to them is embarrassing and
# wastes LLM quota. We drop them entirely (no DB row, no AI trigger).
#
# Names cover the most common system "senders" both ZH/EN. Content patterns
# catch the weekly summary banner and other recurring platform pushes.
_SYSTEM_CONTACT_NAMES: set[str] = {
    "微信团队",
    "企业微信",
    "企业微信团队",
    "腾讯企业微信",
    "腾讯客服",
    "系统消息",
    "系统通知",
    "微信",
    "Weixin Team",
    "WeCom",
    "WeCom Team",
    "Tencent",
}

_SYSTEM_CONTENT_PATTERNS = [
    re.compile(r"查收.{0,4}企业微信.{0,4}(周|月|日)小结"),
    re.compile(r"^.{0,10}企业微信.{0,10}(更新|升级|发版|版本).{0,30}$"),
    re.compile(r"^.{0,10}(登录提醒|安全提醒|异地登录).{0,40}$"),
    re.compile(r"^.{0,10}(账号|密码).{0,4}(修改|重置|变更).{0,30}$"),
    re.compile(r"群发助手|审批结果|打卡提醒|日报提醒"),
]


def _clean_content(text: str) -> str:
    return _AGG_PREFIX_RX.sub("", text or "").strip()


def _is_wecom_system_message(sender_name: str, content: str) -> bool:
    name = (sender_name or "").strip()
    if name in _SYSTEM_CONTACT_NAMES:
        return True
    if not content:
        return False
    for rx in _SYSTEM_CONTENT_PATTERNS:
        if rx.search(content):
            return True
    return False

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

    # IMPORTANT: server time only.
    # We used to honour evt.sent_at (client clock) and that meant a phone
    # whose clock was a few seconds ahead would file the inbound *after*
    # the AI reply that was generated milliseconds later on the server.
    # Client timestamps are kept in the wire schema for analytics but
    # MUST NOT be used for ordering.
    now = utcnow()
    cleaned = _clean_content(evt.content)
    if not cleaned:
        return None  # nothing left after stripping notification noise
    if _TIME_ONLY_RX.match(cleaned):
        return None  # chat timestamp separators are not customer messages
    if _BOT_ECHO_RX.match(cleaned):
        return None  # our own confirmation template echoed back from UI scraping
    if _is_wecom_system_message(evt.contact.nickname, cleaned):
        # WeCom platform notification — don't persist, don't reply.
        import logging
        logging.info(
            "skip WeCom system message from %r: %r",
            evt.contact.nickname, cleaned[:60],
        )
        return None
    msg = Message(
        conversation_id=conv.id,
        direction="in",
        sender_type="customer",
        type=evt.type,
        content=cleaned,
        external_msg_id=evt.external_msg_id,
        created_at=now,
    )
    db.add(msg)

    conv.unread_count = (conv.unread_count or 0) + 1
    conv.last_message_at = now
    conv.last_message_preview = cleaned[:200]

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

    # MVP2: trigger AI auto-reply when mode allows.
    # Per-conversation lock: if a previous burst is still being processed, we
    # queue here briefly and exit when the lock-holder has already covered our
    # message (via the unreplied-chain mechanism).
    if conv.mode in ("ai", "mixed"):
        lock = _lock_for(conv.id)
        async with lock:
            # Did the previous lock-holder already reply past this inbound?
            # If yes, skip — our content is already covered in their batch.
            if await _has_been_replied_after(db, conv.id, msg.created_at):
                import logging
                logging.info(
                    "[conv %s] inbound msg %s already covered by a previous batch — skipping",
                    conv.id, msg.id,
                )
            else:
                try:
                    decision = await ai_workflow.handle_inbound(
                        db, robot=robot, conv=conv, message=msg
                    )
                except Exception:
                    import logging
                    logging.exception("AI workflow failed")
                    decision = None

                if decision is not None:
                    await ai_workflow.broadcast_kb_hits(robot.team_id, conv.id, decision)
                    if decision.action == "reply":
                        for text in decision.all_texts:
                            await create_and_dispatch_send_text(
                                db,
                                robot=robot,
                                conv=conv,
                                contact_external_id=contact.external_id,
                                text=text,
                                sender_type="ai",
                                sender_id=None,
                            )
                            # Tiny gap between bubbles so the device executor
                            # doesn't try to nav→input→send all at once.
                            if len(decision.all_texts) > 1:
                                await asyncio.sleep(0.2)
                    elif decision.action == "suggest":
                        await ai_workflow.broadcast_suggestion(
                            robot.team_id, conv.id, decision
                        )

    # MVP3: refresh long-term memory in the background of the request
    try:
        await summarizer.maybe_refresh(db, contact=contact, conv=conv)
    except Exception:
        import logging
        logging.exception("memory refresh failed")

    return msg
