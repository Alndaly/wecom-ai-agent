"""Long-term memory summarizer.

Strategy: every N new inbound messages (configurable), regenerate the
contact's running summary from the last K messages via the LLM, persist it
on `user_profiles`, and write a vector-indexed entry to `user_memories`
for future semantic retrieval.

Synchronous (`await ...`) for MVP3; can be promoted to Celery later without
changing the call sites.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.providers import ChatMessage, get_provider
from app.core.config import settings
from app.core.db import SessionLocal
from app.kb.embeddings import get_embedding_provider
from app.models import Contact, Conversation, Message, UserMemory, UserProfile, utcnow

log = logging.getLogger(__name__)

# Per-contact lock so two concurrent refresh tasks for the same contact
# serialize — UserProfile.summary / UserMemory writes must not race (different
# contacts on the same robot are independent, so this is per-contact, not
# per-robot).
_contact_locks: dict[int, asyncio.Lock] = {}


def _lock_for(contact_id: int) -> asyncio.Lock:
    lk = _contact_locks.get(contact_id)
    if lk is None:
        lk = asyncio.Lock()
        _contact_locks[contact_id] = lk
    return lk


def schedule_refresh(contact_id: int, conv_id: int) -> None:
    """Fire-and-forget memory refresh.

    Callers don't await — the task runs in the background with its own DB
    session so it can outlive the calling request. If a refresh for the same
    contact is already running we skip (it'll see the new messages in its own
    window), avoiding queue buildup.
    """
    if not settings.memory_refresh_enabled:
        return
    asyncio.create_task(
        _run_refresh(contact_id, conv_id), name=f"memory-refresh-{contact_id}"
    )


async def _run_refresh(contact_id: int, conv_id: int) -> None:
    lock = _lock_for(contact_id)
    if lock.locked():
        log.debug("memory.refresh skip already-running contact=%s", contact_id)
        return
    async with lock:
        try:
            async with SessionLocal() as db:
                contact = await db.get(Contact, contact_id)
                conv = await db.get(Conversation, conv_id)
                if contact is not None and conv is not None:
                    await maybe_refresh(db, contact=contact, conv=conv)
        except Exception:
            log.exception(
                "memory.refresh failed contact=%s conv=%s", contact_id, conv_id
            )

_SUMMARY_PROMPT = (
    "你是私域客服的记忆助理。请阅读以下「客户 ↔ 客服」对话历史,提炼一段 ≤ 120 字"
    "的客户画像摘要,包含:核心诉求、当前阶段、偏好或顾虑、是否高意向。用中文,"
    "不要加引号或解释,只输出摘要本身。"
)


async def maybe_refresh(
    db: AsyncSession, *, contact: Contact, conv: Conversation
) -> UserProfile | None:
    """Decide whether to refresh memory; return the updated profile if refreshed."""
    profile = await db.get(UserProfile, contact.id)

    inbound_count = await _count_inbound(db, conv.id)
    last_id = profile.last_summary_message_id if profile else 0
    new_inbound = await _count_inbound_after(db, conv.id, last_id or 0)

    if new_inbound < settings.memory_summary_every:
        return None

    msgs = await _recent_window(db, conv.id, limit=settings.memory_summary_every * 4)
    transcript = _format_transcript(msgs)
    if not transcript:
        return None

    summary, _ = await _summarize(db, contact.team_id, transcript)

    if profile is None:
        profile = UserProfile(
            contact_id=contact.id,
            team_id=contact.team_id,
            summary=summary,
            stage=contact.stage,
        )
        db.add(profile)
    else:
        profile.summary = summary
        profile.stage = contact.stage

    profile.last_summary_message_id = msgs[-1].id if msgs else last_id
    profile.updated_at = utcnow()

    # vector entry for semantic recall
    try:
        embed = await (await get_embedding_provider(db, contact.team_id)).embed_one(summary)
    except Exception:
        embed = None
    db.add(
        UserMemory(
            contact_id=contact.id,
            team_id=contact.team_id,
            kind="summary",
            content=summary,
            embedding_json=embed,
        )
    )
    await db.commit()
    log.info(
        "memory.refresh contact=%s inbound_total=%s summary=%s",
        contact.id, inbound_count, summary,
    )
    return profile


async def _summarize(db: AsyncSession, team_id: int, transcript: str) -> tuple[str, float]:
    provider = await get_provider(db, team_id)
    res = await provider.chat(
        [
            ChatMessage(role="system", content=_SUMMARY_PROMPT),
            ChatMessage(role="user", content=transcript),
        ],
        temperature=0.2,
        max_tokens=256,
    )
    text = res.text.strip()
    # cleanup: drop trailing punctuation noise from mock provider templates
    text = text.replace("收到您说的「", "").replace("」,我马上确认一下。", "").strip()
    if not text:
        text = transcript
    return text, res.confidence


async def _count_inbound(db: AsyncSession, conv_id: int) -> int:
    rows = (
        await db.execute(
            select(Message.id).where(Message.conversation_id == conv_id, Message.direction == "in")
        )
    ).all()
    return len(rows)


async def _count_inbound_after(db: AsyncSession, conv_id: int, after_msg_id: int) -> int:
    rows = (
        await db.execute(
            select(Message.id).where(
                Message.conversation_id == conv_id,
                Message.direction == "in",
                Message.id > after_msg_id,
            )
        )
    ).all()
    return len(rows)


async def _recent_window(db: AsyncSession, conv_id: int, limit: int) -> list[Message]:
    rows = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conv_id)
            .order_by(desc(Message.id))
            .limit(limit)
        )
    ).scalars().all()
    return list(reversed(rows))


def _format_transcript(messages: list[Message]) -> str:
    out: list[str] = []
    for m in messages:
        speaker = "客户" if m.direction == "in" else ("AI" if m.sender_type == "ai" else "客服")
        out.append(f"{speaker}: {m.content}")
    return "\n".join(out)
