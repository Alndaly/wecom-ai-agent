from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps import current_user
from app.models import Contact, UserMemory, UserProfile, User

router = APIRouter(prefix="/memory", tags=["memory"])


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    contact_id: int
    team_id: int
    summary: str
    stage: str
    updated_at: datetime


class MemoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    contact_id: int
    kind: str
    content: str
    created_at: datetime


async def _authorize(db: AsyncSession, contact_id: int, team_id: int) -> Contact:
    contact = await db.get(Contact, contact_id)
    if not contact or contact.team_id != team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "contact not found")
    return contact


@router.get("/{contact_id}", response_model=ProfileOut | None)
async def get_profile(
    contact_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    await _authorize(db, contact_id, user.team_id)
    prof = await db.get(UserProfile, contact_id)
    return prof


@router.get("/{contact_id}/memories", response_model=list[MemoryOut])
async def list_memories(
    contact_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await _authorize(db, contact_id, user.team_id)
    rows = (
        await db.execute(
            select(UserMemory).where(UserMemory.contact_id == contact_id).order_by(UserMemory.id.desc())
        )
    ).scalars().all()
    return list(rows)
