from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.deps import current_user
from app.models import AIPrompt, AIReplyLog, User

router = APIRouter(prefix="/ai", tags=["ai"])


# ---- schemas ----
class PromptIn(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1)


class PromptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    key: str
    content: str
    version: int
    updated_at: datetime


class ReplyLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    conversation_id: int
    message_id: int | None
    trace_id: str
    action: str
    text: str | None
    confidence: float
    model: str
    latency_ms: int
    reason: str
    created_at: datetime


# ---- prompts ----
@router.get("/prompts", response_model=list[PromptOut])
async def list_prompts(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> list[AIPrompt]:
    rows = (
        await db.execute(
            select(AIPrompt).where(AIPrompt.team_id == user.team_id).order_by(AIPrompt.key)
        )
    ).scalars().all()
    return list(rows)


@router.put("/prompts", response_model=PromptOut)
async def upsert_prompt(
    body: PromptIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> AIPrompt:
    row = (
        await db.execute(
            select(AIPrompt).where(AIPrompt.team_id == user.team_id, AIPrompt.key == body.key)
        )
    ).scalar_one_or_none()
    if row is None:
        row = AIPrompt(team_id=user.team_id, key=body.key, content=body.content, version=1)
        db.add(row)
    else:
        row.content = body.content
        row.version += 1
    await db.commit()
    await db.refresh(row)
    return row


@router.get("/prompts/default", response_model=PromptOut | None)
async def get_default_prompt(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> AIPrompt | None:
    row = (
        await db.execute(
            select(AIPrompt).where(AIPrompt.team_id == user.team_id, AIPrompt.key == "default")
        )
    ).scalar_one_or_none()
    return row


# ---- logs ----
@router.get("/logs", response_model=list[ReplyLogOut])
async def list_logs(
    conversation_id: int | None = None,
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AIReplyLog]:
    stmt = select(AIReplyLog).where(AIReplyLog.team_id == user.team_id)
    if conversation_id is not None:
        stmt = stmt.where(AIReplyLog.conversation_id == conversation_id)
    stmt = stmt.order_by(desc(AIReplyLog.id)).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.get("/info")
async def info(user: User = Depends(current_user)) -> dict:
    return {
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "confidence_threshold": settings.ai_confidence_threshold,
        "default_prompt_fallback": settings.ai_default_prompt,
    }
