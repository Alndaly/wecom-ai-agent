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

from .providers import ChatMessage, get_fallback_provider, get_provider

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
    # Multi-reply: the agent may emit several short messages instead of one
    # paragraph. When empty, callers should fall back to [text] for compat.
    replies: list[str] = field(default_factory=list)

    @property
    def all_texts(self) -> list[str]:
        if self.replies:
            return self.replies
        return [self.text] if self.text else []


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
    # All inbound messages from the customer that haven't been replied to yet —
    # populated by `_load_unreplied_chain`. If the customer fired 5 messages
    # while we weren't looking, the agent reasons over all 5 in one shot
    # instead of producing 5 independent (and possibly contradictory) replies.
    unreplied_chain: list[Message] = field(default_factory=list)


async def handle_inbound(
    db: AsyncSession, *, robot: Robot, conv: Conversation, message: Message
) -> Decision:
    """Entry point. Always returns a Decision; persists an AIReplyLog row."""
    state = AIState(conv=conv, robot=robot, inbound=message)
    log.info(
        "[workflow] inbound conv=%s mode=%s trace=%s text=%r",
        conv.id, conv.mode, state.trace_id, message.content,
    )

    # 1. mode gate
    if conv.mode == "human":
        d = Decision(action="skip", reason="mode=human", trace_id=state.trace_id)
        await _finalize(db, state, d)
        log.info("[workflow] skip (mode=human) trace=%s", state.trace_id)
        return d

    ai_cfg = await settings_service.get(db, conv.team_id, "ai")
    state.context_window = int(ai_cfg.get("context_window") or settings.ai_context_window)
    state.confidence_threshold = float(
        ai_cfg.get("confidence_threshold") or settings.ai_confidence_threshold
    )

    # 2. context
    state.prompt = await _load_prompt(db, conv.team_id, ai_cfg)
    state.memory_summary = await _load_memory(db, conv.contact_id)
    state.unreplied_chain = await _load_unreplied_chain(db, conv.id)
    state.history = await _load_history(db, conv.id, state.context_window)
    # Keep current-turn messages out of the history window. They are added
    # below as a clearly labelled "unreplied" bundle. If the same text appears
    # in both places the LLM tends to miscount history as "the user asked three
    # times", which is exactly the duplicate-reply failure we are fixing.
    unreplied_ids = {m.id for m in state.unreplied_chain}
    if unreplied_ids:
        state.history = [m for m in state.history if m.id not in unreplied_ids]
    # If the customer fired several messages in a row, run retrieval on the
    # concatenated text so the KB step sees the full intent, not just the
    # latest fragment ("还有，价格怎么算？" alone is uninformative).
    retrieval_query = " ".join(m.content for m in state.unreplied_chain) or message.content
    state.retrieval = await _retrieve(db, conv.team_id, retrieval_query)

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
    log.info(
        "[workflow] decision action=%s conf=%.2f model=%s latency=%dms reason=%s",
        decision.action, decision.confidence, decision.model, decision.latency_ms, decision.reason,
    )
    return decision


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------
async def _load_unreplied_chain(db: AsyncSession, conversation_id: int) -> list[Message]:
    """Return customer messages still awaiting feedback, in chrono order."""
    stmt = (
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == "in",
            Message.sender_type == "customer",
            Message.feedback_status.in_(("pending", "processing")),
        )
        .order_by(Message.created_at.asc())
        .limit(20)  # hard cap to keep prompt bounded
    )
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


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
    fallback_provider = await get_fallback_provider(db, team_id)
    llm_cfg = await settings_service.get(db, team_id, "llm")
    ai_cfg = await settings_service.get(db, team_id, "ai")

    # ReAct agent path: lets the model call tools (kb_search, set_profile_field,
    # MCP, user skills) before producing the final reply.
    if _agent_enabled(ai_cfg):
        return await _generate_via_agent(state, db, provider, fallback_provider, llm_cfg, ai_cfg)

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
        label = "历史客户消息（已处理，仅供背景）" if role == "user" else "历史客服回复（已发送，仅供背景）"
        msgs.append(ChatMessage(role=role, content=f"【{label}】{m.content}"))
    chain_texts = [m.content for m in state.unreplied_chain] or [state.inbound.content]
    msgs.append(ChatMessage(role="user", content=_format_unreplied_turn(chain_texts)))
    try:
        result = await provider.chat(
            msgs,
            temperature=float(llm_cfg.get("temperature", settings.llm_temperature)),
            max_tokens=int(ai_cfg.get("max_tokens") or settings.ai_max_tokens),
        )
    except Exception as e:
        if fallback_provider is None:
            log.exception("LLM error")
            return Decision(
                action="suggest",
                text=None,
                confidence=0.0,
                trace_id=state.trace_id,
                reason=f"llm_error: {e}",
            )
        log.warning("primary LLM error, trying fallback: %s", e)
        result = await fallback_provider.chat(
            msgs,
            temperature=float(llm_cfg.get("temperature", settings.llm_temperature)),
            max_tokens=int(ai_cfg.get("max_tokens") or settings.ai_max_tokens),
        )

    if fallback_provider is not None and _llm_result_insufficient(result.text, result.confidence):
        log.info("primary LLM looks insufficient, trying fallback model=%s", getattr(fallback_provider, "model", "?"))
        try:
            fallback_result = await fallback_provider.chat(
                msgs,
                temperature=float(llm_cfg.get("temperature", settings.llm_temperature)),
                max_tokens=int(ai_cfg.get("max_tokens") or settings.ai_max_tokens),
            )
            if not _llm_result_insufficient(fallback_result.text, fallback_result.confidence):
                result = fallback_result
        except Exception:
            log.exception("fallback LLM failed; keeping primary result")

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


def _agent_enabled(ai_cfg: dict) -> bool:
    """Per-team toggle, falls back to env default. Stored under `ai.agent_mode`
    so adding a UI switch later is a one-line settings change."""
    val = ai_cfg.get("agent_mode")
    if val is None:
        return settings.agent_mode_enabled
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _format_unreplied_turn(chain: list[str]) -> str:
    cleaned = [c for c in chain if c]
    if not cleaned:
        return "【本轮未回复消息】（空）"
    if len(cleaned) == 1:
        return f"【本轮未回复消息，共 1 条】\n[1] {cleaned[0]}"
    lines = [f"【本轮未回复消息，共 {len(cleaned)} 条，按时间从早到晚】"]
    lines.extend(f"[{i}] {content}" for i, content in enumerate(cleaned, 1))
    lines.append("请只基于本轮未回复消息判断客户当前是否连续发送、是否需要合并回复。")
    return "\n".join(lines)


def _llm_result_insufficient(text: str, confidence: float) -> bool:
    body = (text or "").strip()
    if not body:
        return True
    if confidence < 0.45:
        return True
    weak_markers = (
        "不知道", "不清楚", "无法回答", "不能回答", "无法确定", "没有相关信息",
        "知识库中没有", "稍后再试", "出了点小问题", "抱歉，我这边",
    )
    if len(body) < 20 and any(m in body for m in weak_markers):
        return True
    return any(m in body for m in weak_markers[:8]) and len(body) < 120


async def _generate_via_agent(
    state: AIState, db: AsyncSession, provider, fallback_provider, llm_cfg: dict, ai_cfg: dict
) -> Decision:
    from .conv_agent import run_conv_agent

    system_parts = [state.prompt]
    if state.memory_summary:
        system_parts.append(f"【客户画像】{state.memory_summary}")
    # Pre-retrieval context is still nice as a *hint*, but the agent is free
    # to re-query via kb_search if it needs more.
    kb_block = state.retrieval.to_context()
    if kb_block:
        system_parts.append(kb_block)
    system_prompt = "\n\n".join(system_parts)

    # Pass the full unreplied chain. The agent decides whether to answer
    # them all in one message or split into several.
    chain_texts = [m.content for m in state.unreplied_chain] or [state.inbound.content]
    res = await run_conv_agent(
        db=db,
        team_id=state.conv.team_id,
        conversation_id=state.conv.id,
        contact_id=state.conv.contact_id,
        inbound_text=state.inbound.content,
        trace_id=state.trace_id,
        system_prompt=system_prompt,
        profile_summary=state.memory_summary,
        history=state.history,
        unreplied_chain=chain_texts,
        provider=provider,
        fallback_provider=fallback_provider,
        temperature=float(llm_cfg.get("temperature", settings.llm_temperature)),
        max_tokens=int(ai_cfg.get("max_tokens") or settings.ai_max_tokens),
        max_steps=int(ai_cfg.get("agent_max_steps") or settings.agent_max_steps),
    )

    # Merge agent-discovered KB hits with the pre-retrieval set so the Web
    # right-panel shows everything the agent actually read.
    pre_ids = [h.chunk_id for h in state.retrieval.hits]
    merged_ids = list(dict.fromkeys(pre_ids + (res.kb_hit_ids or [])))
    state.retrieval.hits = state.retrieval.hits  # noqa — silences linter, semantics unchanged

    if res.escalate:
        return Decision(
            action="suggest",
            text=None,
            confidence=0.0,
            trace_id=state.trace_id,
            reason=f"agent_escalate: {res.escalate_reason}",
            latency_ms=res.latency_ms,
            model=res.model,
            kb_hit_ids=merged_ids,
            kb_context=res.kb_context or state.retrieval.to_context(),
        )

    return Decision(
        action="reply",
        text=(res.text or "").strip(),
        replies=list(res.replies or []),
        confidence=res.confidence,
        trace_id=state.trace_id,
        latency_ms=res.latency_ms,
        model=res.model,
        kb_hit_ids=merged_ids,
        kb_context=res.kb_context or state.retrieval.to_context(),
        reason=f"agent_steps={res.steps}",
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
