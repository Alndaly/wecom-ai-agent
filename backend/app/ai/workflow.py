"""AI auto-reply workflow.

Hand-rolled state-machine (LangGraph-shaped). Each node is an async function
operating on `AIState`. Replaceable with LangGraph later — node signatures
won't change.

Decision outcomes:
  - reply    → AI sends a message
  - suggest  → AI proposes drafts to human (mixed mode, low confidence)
  - skip     → AI does nothing (mode=human, etc.)

MVP3 adds two upstream nodes:
  - `load_memory`  : pull `UserProfile.summary` for the contact
  - `retrieve`     : KB vector + graph search via app.kb.retriever
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.ws_manager import hub
from app.kb.retriever import RetrievalResult, retrieve
from app.models import (
    AIPrompt,
    AIReplyLog,
    Conversation,
    Message,
    Robot,
    UserProfile,
    utcnow,
)
from app.services import settings_service

from .providers import ChatMessage, get_provider

log = logging.getLogger(__name__)


Action = Literal["reply", "suggest", "skip"]


@dataclass
class Decision:
    action: Action
    text: str | None = None
    confidence: float = 0.0
    trace_id: str = ""
    reason: str = ""
    latency_ms: int = 0
    model: str = ""
    # MVP3 — surfaced to the Web client as ai.suggestion / kb.hits
    kb_hit_ids: list[int] = field(default_factory=list)
    kb_context: str = ""
    memory_summary: str = ""


@dataclass
class AIState:
    conv: Conversation
    robot: Robot
    inbound: Message
    history: list[Message] = field(default_factory=list)
    prompt: str = ""
    memory_summary: str = ""
    retrieval: RetrievalResult = field(default_factory=RetrievalResult)
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    # team-scoped runtime config (loaded once per request)
    context_window: int = 10
    confidence_threshold: float = 0.55


async def handle_inbound(
    db: AsyncSession, *, robot: Robot, conv: Conversation, message: Message
) -> Decision:
    """Entry point. Always returns a Decision; persists an AIReplyLog row."""
    state = AIState(conv=conv, robot=robot, inbound=message)

    # 1. mode gate
    if conv.mode == "human":
        d = Decision(action="skip", reason="mode=human", trace_id=state.trace_id)
        await _finalize(db, state, d)
        return d

    ai_cfg = await settings_service.get(db, conv.team_id, "ai")
    state.context_window = int(ai_cfg.get("context_window") or settings.ai_context_window)
    state.confidence_threshold = float(
        ai_cfg.get("confidence_threshold") or settings.ai_confidence_threshold
    )

    # 2. context
    state.history = await _load_history(db, conv.id, state.context_window)
    state.prompt = await _load_prompt(db, conv.team_id, ai_cfg)
    state.memory_summary = await _load_memory(db, conv.contact_id)
    state.retrieval = await _retrieve(db, conv.team_id, message.content)

    # 3. generate
    decision = await _generate(state, db)
    decision.memory_summary = state.memory_summary
    decision.kb_hit_ids = [h.chunk_id for h in state.retrieval.hits]
    decision.kb_context = state.retrieval.to_context()

    # 4. confidence gate → escalate to human if mixed mode + low confidence
    if (
        decision.action == "reply"
        and conv.mode == "mixed"
        and decision.confidence < state.confidence_threshold
    ):
        decision.action = "suggest"
        decision.reason = (
            f"confidence {decision.confidence:.2f} < threshold {state.confidence_threshold}"
        )

    await _finalize(db, state, decision)
    return decision


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------
async def _load_history(db: AsyncSession, conversation_id: int, limit: int) -> list[Message]:
    rows = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(desc(Message.created_at))
            .limit(limit)
        )
    ).scalars().all()
    return list(reversed(rows))


async def _load_prompt(db: AsyncSession, team_id: int, ai_cfg: dict) -> str:
    """Prefer team-saved prompt in ai_prompts, then settings UI default, then env."""
    row = (
        await db.execute(
            select(AIPrompt).where(AIPrompt.team_id == team_id, AIPrompt.key == "default")
        )
    ).scalar_one_or_none()
    if row and row.content:
        return row.content
    cfg_prompt = (ai_cfg.get("default_prompt") or "").strip()
    return cfg_prompt or settings.ai_default_prompt


async def _load_memory(db: AsyncSession, contact_id: int) -> str:
    prof = await db.get(UserProfile, contact_id)
    return (prof.summary or "") if prof else ""


async def _retrieve(db: AsyncSession, team_id: int, query: str) -> RetrievalResult:
    try:
        return await retrieve(db, team_id=team_id, query=query)
    except Exception:
        log.exception("retrieve failed; continuing with empty context")
        return RetrievalResult()


async def _generate(state: AIState, db: AsyncSession) -> Decision:
    team_id = state.conv.team_id
    provider = await get_provider(db, team_id)
    llm_cfg = await settings_service.get(db, team_id, "llm")
    ai_cfg = await settings_service.get(db, team_id, "ai")

    system_parts = [state.prompt]
    if state.memory_summary:
        system_parts.append(f"【客户画像】{state.memory_summary}")
    kb_block = state.retrieval.to_context()
    if kb_block:
        system_parts.append(kb_block)
    system_parts.append(
        "请基于上述知识库片段与客户画像回答(若片段不足以回答,请坦诚说明并降低置信)。"
    )

    msgs: list[ChatMessage] = [ChatMessage(role="system", content="\n\n".join(system_parts))]
    for m in state.history:
        role = "user" if m.direction == "in" else "assistant"
        msgs.append(ChatMessage(role=role, content=m.content))
    try:
        result = await provider.chat(
            msgs,
            temperature=float(llm_cfg.get("temperature", settings.llm_temperature)),
            max_tokens=int(ai_cfg.get("max_tokens") or settings.ai_max_tokens),
        )
    except Exception as e:
        log.exception("LLM error")
        return Decision(
            action="suggest",
            text=None,
            confidence=0.0,
            trace_id=state.trace_id,
            reason=f"llm_error: {e}",
        )

    # boost confidence a notch if KB clearly contributed
    conf = result.confidence
    if state.retrieval.hits and conf < 0.85:
        top = max(h.score for h in state.retrieval.hits)
        conf = min(0.95, conf + 0.15 * top)

    return Decision(
        action="reply",
        text=result.text.strip(),
        confidence=conf,
        trace_id=state.trace_id,
        latency_ms=result.latency_ms,
        model=result.model,
    )


async def _finalize(db: AsyncSession, state: AIState, decision: Decision) -> None:
    db.add(
        AIReplyLog(
            team_id=state.conv.team_id,
            conversation_id=state.conv.id,
            message_id=state.inbound.id,
            trace_id=decision.trace_id,
            action=decision.action,
            text=decision.text,
            confidence=decision.confidence,
            model=decision.model,
            latency_ms=decision.latency_ms,
            reason=decision.reason,
            created_at=utcnow(),
        )
    )
    await db.commit()


# ---------------------------------------------------------------------------
# side effects (called by services/conversation.py after decision returns)
# ---------------------------------------------------------------------------
async def broadcast_suggestion(team_id: int, conversation_id: int, decision: Decision) -> None:
    if decision.action != "suggest" or not decision.text:
        return
    await hub.broadcast_web(
        team_id,
        "ai.suggestion",
        {
            "conversation_id": conversation_id,
            "suggestions": [
                {
                    "text": decision.text,
                    "confidence": decision.confidence,
                    "trace_id": decision.trace_id,
                    "kb_hit_ids": decision.kb_hit_ids,
                }
            ],
        },
    )


async def broadcast_kb_hits(team_id: int, conversation_id: int, decision: Decision) -> None:
    """After any AI decision with KB hits, push them to the Web right panel."""
    if not decision.kb_hit_ids:
        return
    await hub.broadcast_web(
        team_id,
        "kb.hits",
        {
            "conversation_id": conversation_id,
            "hit_ids": decision.kb_hit_ids,
            "trace_id": decision.trace_id,
        },
    )
