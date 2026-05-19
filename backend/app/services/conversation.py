"""Conversation + message-gateway service.

Handles the inbound side (Android → backend) and broadcasts to Web hub.
"""
from __future__ import annotations

import re
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.ws_manager import hub
from app.memory import summarizer
from app.models import Contact, Conversation, Message, Robot, RobotTask, RobotTaskLog, utcnow
from app.schemas import (
    AndroidMessageReceived,
    ConversationOut,
    MessageOut,
)

log = logging.getLogger(__name__)


def _normalize_message_content(content: str) -> str:
    return " ".join((content or "").strip().split())


def _looks_like_same_long_message(a: str, b: str) -> bool:
    left = _normalize_message_content(a)
    right = _normalize_message_content(b)
    if not left or not right:
        return False
    if left == right:
        return True
    # Accessibility may report the same outgoing bubble twice: once with the
    # full text and once clipped to the visible line range. Treat long prefix
    # matches as the same self/outbound message, but avoid collapsing short
    # test messages such as "1", "2", "12".
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    return len(shorter) >= 40 and longer.startswith(shorter)


async def _has_recent_outbound_same_content(
    db: AsyncSession, conv_id: int, content: str, now: datetime
) -> bool:
    rows = (
        await db.execute(
            select(Message.content)
            .where(
                Message.conversation_id == conv_id,
                Message.direction == "out",
                Message.created_at > now - timedelta(minutes=3),
            )
            .order_by(Message.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    return any(_looks_like_same_long_message(existing, content) for existing in rows)


async def _has_recent_inbound_same_content(
    db: AsyncSession,
    conv_id: int,
    content: str,
    now: datetime,
    external_msg_id: str | None,
    window_seconds: int = 90,
) -> bool:
    current_source = _source_from_external_msg_id(external_msg_id)
    row = (
        await db.execute(
            select(Message.external_msg_id)
            .where(
                Message.conversation_id == conv_id,
                Message.direction == "in",
                Message.sender_type == "customer",
                Message.content == content,
                Message.created_at > now - timedelta(seconds=window_seconds),
            )
            .limit(1)
        )
    ).scalars().all()
    for previous_external_msg_id in row:
        previous_source = _source_from_external_msg_id(previous_external_msg_id)
        if current_source is None or previous_source is None:
            return True
        if current_source != previous_source:
            return True
    return False


async def _is_cross_channel_replay(
    db: AsyncSession,
    conv_id: int,
    content: str,
    now: datetime,
    external_msg_id: str | None,
) -> bool:
    current_source = _source_from_external_msg_id(external_msg_id)
    if current_source not in {"a11y", "notif"}:
        return False
    rows = (
        await db.execute(
            select(Message.external_msg_id)
            .where(
                Message.conversation_id == conv_id,
                Message.direction == "in",
                Message.sender_type == "customer",
                Message.content == content,
                Message.created_at > now - timedelta(minutes=5),
            )
            .order_by(Message.created_at.desc())
            .limit(10)
        )
    ).scalars().all()
    for previous_external_msg_id in rows:
        previous_source = _source_from_external_msg_id(previous_external_msg_id)
        if previous_source in {"a11y", "notif"} and previous_source != current_source:
            return True
    return False


def _source_from_external_msg_id(external_msg_id: str | None) -> str | None:
    if not external_msg_id or ":" not in external_msg_id:
        return None
    source = external_msg_id.split(":", 1)[0]
    if source in {"notif", "a11y", "a11y-self"}:
        return source
    return None


def _external_message_id_kind(external_msg_id: str | None) -> str:
    if not external_msg_id:
        return "none"
    parts = external_msg_id.split(":", 2)
    if len(parts) >= 2 and parts[1].startswith("stable"):
        return "stable"
    if len(parts) >= 2 and parts[1].isdigit():
        return "post_time"
    return "unknown"


async def _queue_chat_harvest_for_conversation_list_preview(
    db: AsyncSession,
    *,
    robot: Robot,
    conversation: Conversation,
    contact_name: str,
    preview: str,
    unread_count: int,
) -> bool:
    # The conversation list only exposes the last unread message. When the
    # unread badge says there are multiple messages, open the chat first so the
    # accessibility collector can harvest the complete message bubbles.
    active_tasks = (
        await db.execute(
            select(RobotTask)
            .where(
                RobotTask.robot_id == robot.id,
                RobotTask.conversation_id == conversation.id,
                RobotTask.type == "agent_goal",
                RobotTask.status.in_(("pending", "dispatched", "queued", "running")),
            )
            .order_by(RobotTask.created_at.desc())
            .limit(10)
        )
    ).scalars().all()
    existing = next(
        (
            task
            for task in active_tasks
            if (task.payload_json or {}).get("reason") == "conversation_list_preview_multiple_unread"
        ),
        None,
    )
    if existing:
        log.info(
            "[message-callback] event=chat_harvest outcome=skip reason=task_already_active "
            "robot=%s conversation_id=%s task_id=%s unread_count=%s preview_text=%r",
            robot.robot_id,
            conversation.id,
            existing.id,
            unread_count,
            preview,
        )
        return True

    goal = f"打开与「{contact_name}」的聊天，等待当前聊天页消息采集完成；不要发送任何内容。"
    task = RobotTask(
        robot_id=robot.id,
        type="agent_goal",
        payload_json={
            "goal": goal,
            "max_steps": 6,
            "reason": "conversation_list_preview_multiple_unread",
            "unread_count": unread_count,
            "preview_text": preview,
        },
        status="dispatched",
        priority=50,
        conversation_id=conversation.id,
    )
    db.add(task)
    await db.flush()
    db.add(
        RobotTaskLog(
            robot_id=robot.id,
            task_id=task.id,
            level="info",
            message=(
                "会话列表预览显示多条未读，先打开聊天页采集完整消息气泡 "
                f"unread_count={unread_count} preview_text={preview!r}"
            ),
        )
    )
    await db.commit()

    from app.services import task_queue

    await task_queue.enqueue(
        robot.robot_id,
        "agent_goal",
        task.id,
        priority=task_queue.PRIORITY_AUTO_REPLY,
    )
    log.info(
        "[message-callback] event=chat_harvest outcome=queued robot=%s conversation_id=%s task_id=%s "
        "reason=conversation_list_preview_multiple_unread unread_count=%s preview_text=%r",
        robot.robot_id,
        conversation.id,
        task.id,
        unread_count,
        preview,
    )
    return True


async def _has_active_conversation_list_harvest(
    db: AsyncSession, *, robot_id: int, conversation_id: int
) -> bool:
    active_tasks = (
        await db.execute(
            select(RobotTask)
            .where(
                RobotTask.robot_id == robot_id,
                RobotTask.conversation_id == conversation_id,
                RobotTask.type == "agent_goal",
                RobotTask.status.in_(("pending", "dispatched", "queued", "running")),
            )
            .order_by(RobotTask.created_at.desc())
            .limit(10)
        )
    ).scalars().all()
    return any(
        (task.payload_json or {}).get("reason")
        == "conversation_list_preview_multiple_unread"
        for task in active_tasks
    )

# WeCom aggregates >1 pending unread messages in a single chat into a single
# notification whose body is prefixed with "[N条]". The Android listener strips
# it, but we mirror that here as a defence-in-depth — other inbound channels
# (REST replay, third-party recorders) may forward the raw text.
_AGG_PREFIX_RX = re.compile(r"^\s*\[\s*\d+\s*条\s*]\s*")
_BOT_ECHO_RX = re.compile(r"^\s*收到您说的[「\"']")
_TIME_ONLY_RX = re.compile(r"^\s*(上午|下午)?\s*\d{1,2}:\d{2}\s*$")

# WeCom shows media messages in the conversation-list preview as a bracketed
# placeholder ("[图片]", "[视频]", ...). When the device picks the message up
# from the conversation-list harvest (not from the chat thread), we only get this text and
# no media bytes. Upgrade the type so downstream prompt formatting and vision
# routing treat it as media rather than a literal text question.
_MEDIA_PLACEHOLDER_TO_TYPE: dict[str, str] = {
    "[图片]": "image",
    "[图片消息]": "image",
    "[图片表情]": "image",
    "[Image]": "image",
    "[视频]": "video",
    "[Video]": "video",
}

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

_UNRELIABLE_CONTACT_NAMES: set[str] = {
    "",
    "当前聊天",
    "消息",
    "邮件",
    "文档",
    "工作台",
    "通讯录",
    "设置",
}


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

async def ingest_inbound_message(
    db: AsyncSession, robot: Robot, evt: AndroidMessageReceived
) -> Message | None:
    """Persist an Android-observed chat message; returns Message or None if duplicate.

    Customer messages are inbound and may trigger AI. Human/self messages are
    outbound conversation history only; they are persisted so future answers
    see the full chat, but they never kick off an auto-reply.
    """
    now = utcnow()
    cleaned = _clean_content(evt.content)
    contact_key = (evt.contact.external_id or evt.contact.nickname or "").strip()
    if not cleaned:
        return None  # nothing left after stripping notification noise
    if contact_key in _UNRELIABLE_CONTACT_NAMES:
        log.warning(
            "[message-callback] skipped unreliable contact robot=%s contact=%r content=%r",
            robot.robot_id,
            contact_key,
            cleaned,
        )
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

    original_external_msg_id = evt.external_msg_id
    external_msg_id_status = "stored" if original_external_msg_id else "none"

    # dedupe by external_msg_id (scoped to this conversation — the same hash on a
    # different robot/device is a legitimately distinct message).
    if evt.external_msg_id:
        existing = (
            await db.execute(
                select(Message).where(
                    Message.conversation_id == conv.id,
                    Message.external_msg_id == evt.external_msg_id,
                )
            )
        ).scalar_one_or_none()
        if existing:
            age = now - existing.created_at
            if age <= timedelta(minutes=5):
                log.info(
                    "[message-callback] event=ingest_dedupe outcome=skip reason=duplicate_external_msg_id "
                    "conversation_id=%s existing_message_id=%s age=%s collection_source=%s "
                    "external_id_kind=%s external_msg_id=%s content=%r",
                    existing.conversation_id,
                    existing.id,
                    age,
                    _source_from_external_msg_id(evt.external_msg_id) or "unknown",
                    _external_message_id_kind(evt.external_msg_id),
                    evt.external_msg_id,
                    evt.content or "",
                )
                return None
            log.warning(
                "[message-callback] event=ingest_dedupe outcome=accept reason=stale_external_msg_id_collision "
                "action=drop_external_msg_id conversation_id=%s existing_message_id=%s age=%s stale_after=5m "
                "collection_source=%s external_id_kind=%s original_external_msg_id=%s content=%r",
                conv.id,
                existing.id,
                age,
                _source_from_external_msg_id(evt.external_msg_id) or "unknown",
                _external_message_id_kind(evt.external_msg_id),
                evt.external_msg_id,
                evt.content or "",
            )
            evt.external_msg_id = None
            external_msg_id_status = "dropped_stale_collision"

    # IMPORTANT: server time only.
    # We used to honour evt.sent_at (client clock) and that meant a phone
    # whose clock was a few seconds ahead would file the inbound *after*
    # the AI reply that was generated milliseconds later on the server.
    # Client timestamps are kept in the wire schema for analytics but
    # MUST NOT be used for ordering.
    from_customer = evt.sender_type == "customer"
    if _TIME_ONLY_RX.match(cleaned):
        return None  # chat timestamp separators are not customer messages
    if from_customer and _BOT_ECHO_RX.match(cleaned):
        return None  # our own confirmation template echoed back from UI scraping
    if from_customer and _is_wecom_system_message(evt.contact.nickname, cleaned):
        # WeCom platform notification — don't persist, don't reply.
        log.info(
            "skip WeCom system message from %r: %r",
            evt.contact.nickname, cleaned,
        )
        return None
    if (
        from_customer
        and evt.observation_source == "conversation_list_preview"
        and evt.completeness == "preview_only"
        and int(evt.unread_count or 0) > 1
    ):
        await _queue_chat_harvest_for_conversation_list_preview(
            db,
            robot=robot,
            conversation=conv,
            contact_name=evt.contact.external_id,
            preview=cleaned,
            unread_count=int(evt.unread_count or 0),
        )
        log.info(
            "[message-callback] event=message_ingest outcome=skip "
            "reason=conversation_list_preview_incomplete_multiple_unread robot=%s "
            "conversation_id=%s contact=%s unread_count=%s preview_text=%r",
            robot.robot_id,
            conv.id,
            evt.contact.external_id,
            evt.unread_count,
            cleaned,
        )
        return None
    duplicate_inbound = False
    if from_customer:
        duplicate_inbound = await _is_cross_channel_replay(
            db, conv.id, cleaned, now, evt.external_msg_id
        ) or (
            settings.inbound_content_dedupe_enabled
            and await _has_recent_inbound_same_content(
                db, conv.id, cleaned, now, evt.external_msg_id
            )
        )
    if duplicate_inbound:
        log.info(
            "[message-callback] event=ingest_dedupe outcome=skip reason=recent_same_content_or_cross_channel_replay "
            "conversation_id=%s contact=%s external_msg_id_status=%s collection_source=%s external_id_kind=%s "
            "original_external_msg_id=%s effective_external_msg_id=%s content=%r",
            conv.id,
            evt.contact.external_id,
            external_msg_id_status,
            _source_from_external_msg_id(evt.external_msg_id) or "unknown",
            _external_message_id_kind(evt.external_msg_id),
            original_external_msg_id,
            evt.external_msg_id,
            cleaned,
        )
        return None
    if not from_customer and await _has_recent_outbound_same_content(db, conv.id, cleaned, now):
        log.info(
            "skip self echo already recorded conversation_id=%s contact=%s content=%r",
            conv.id,
            evt.contact.external_id,
            cleaned,
        )
        return None
    effective_type = evt.type
    if (
        from_customer
        and effective_type == "text"
        and evt.media_json is None
        and cleaned in _MEDIA_PLACEHOLDER_TO_TYPE
    ):
        effective_type = _MEDIA_PLACEHOLDER_TO_TYPE[cleaned]
    msg = Message(
        conversation_id=conv.id,
        direction="in" if from_customer else "out",
        sender_type="customer" if from_customer else "human",
        type=effective_type,
        content=cleaned,
        media_json=evt.media_json,
        external_msg_id=evt.external_msg_id,
        created_at=now,
        feedback_status="pending" if from_customer else None,
    )
    db.add(msg)

    if from_customer:
        conv.unread_count = (conv.unread_count or 0) + 1
    conv.last_message_at = now
    conv.last_message_preview = cleaned

    await db.commit()
    await db.refresh(msg)
    await db.refresh(conv)
    # eagerly load contact for serialization
    await db.refresh(conv, attribute_names=["contact"])
    log.info(
        "[message-callback] event=message_persisted outcome=stored robot=%s conversation_id=%s message_id=%s "
        "contact=%s direction=%s sender_type=%s type=%s feedback_status=%s "
        "external_msg_id_status=%s original_external_msg_id=%s stored_external_msg_id=%s "
        "collection_source=%s external_id_kind=%s content=%r",
        robot.robot_id,
        conv.id,
        msg.id,
        evt.contact.external_id,
        msg.direction,
        evt.sender_type,
        msg.type,
        msg.feedback_status,
        external_msg_id_status,
        original_external_msg_id,
        evt.external_msg_id,
        _source_from_external_msg_id(original_external_msg_id) or "unknown",
        _external_message_id_kind(original_external_msg_id),
        cleaned,
    )

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

    harvest_in_progress = from_customer and await _has_active_conversation_list_harvest(
        db, robot_id=robot.id, conversation_id=conv.id
    )
    if not settings.auto_reply_enabled:
        log.info(
            "[message-callback] event=auto_reply outcome=skip reason=global_disabled conversation_id=%s message_id=%s mode=%s",
            conv.id,
            msg.id,
            conv.mode,
        )
    elif harvest_in_progress:
        log.info(
            "[message-callback] event=auto_reply outcome=defer "
            "reason=conversation_list_harvest_in_progress conversation_id=%s message_id=%s mode=%s",
            conv.id,
            msg.id,
            conv.mode,
        )
    elif from_customer and conv.mode in ("ai", "mixed"):
        from app.services import auto_reply_scheduler

        log.info(
            "[message-callback] event=auto_reply outcome=wake robot=%s conversation_id=%s message_id=%s mode=%s source=message.received",
            robot.robot_id,
            conv.id,
            msg.id,
            conv.mode,
        )
        auto_reply_scheduler.wake_robot(robot.id)
    elif from_customer:
        log.info(
            "[message-callback] event=auto_reply outcome=skip reason=conversation_mode conversation_id=%s message_id=%s mode=%s",
            conv.id,
            msg.id,
            conv.mode,
        )

    # MVP3: refresh long-term memory off the request's critical path. The
    # summarizer owns its own DB session and serializes per-contact internally
    # — see app/memory/summarizer.py:schedule_refresh.
    if settings.memory_refresh_enabled:
        summarizer.schedule_refresh(contact.id, conv.id)
    else:
        log.info(
            "[message-callback] memory refresh disabled conversation_id=%s message_id=%s",
            conv.id,
            msg.id,
        )

    return msg
