"""HTTP API for the persona system.

Endpoints (all require an authenticated user — same auth as the rest of
the settings surface):

  GET    /personas              list (id, name, description, chars)
  GET    /personas/{id}         full detail (soul/memory/style content)
  POST   /personas              create
  PUT    /personas/{id}         update one or more sections (PATCH-style)
  DELETE /personas/{id}         delete (refuses 'default')

Validation is strict: ids must be safe slugs (no path separators, no
leading dot), enforced by `personas._validated_id` so the filesystem
can never be addressed outside `app/ai/personas/`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.ai import personas as personas_mod
from app.deps import current_user
from app.models import User

router = APIRouter(prefix="/personas", tags=["personas"])


# ---- request / response shapes -------------------------------------------


class PersonaSummary(BaseModel):
    id: str
    name: str
    description: str
    chars: int
    protected: bool


class PersonaDetail(BaseModel):
    id: str
    name: str
    description: str
    soul: str
    memory: str
    style: str


class PersonaCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    soul: str = ""
    memory: str = ""
    style: str = ""


class PersonaUpdate(BaseModel):
    # All fields optional — PUT acts as PATCH so the editor can save one
    # tab at a time without re-sending the whole document.
    name: str | None = None
    description: str | None = None
    soul: str | None = None
    memory: str | None = None
    style: str | None = None


# ---- routes --------------------------------------------------------------


@router.get("", response_model=list[PersonaSummary])
async def list_all(_: User = Depends(current_user)) -> list[PersonaSummary]:
    return [
        PersonaSummary(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            chars=int(row["chars"]),
            protected=row["protected"] == "true",
        )
        for row in personas_mod.list_personas()
    ]


@router.get("/{persona_id}", response_model=PersonaDetail)
async def get_one(persona_id: str, _: User = Depends(current_user)) -> PersonaDetail:
    detail = personas_mod.get_persona_detail(persona_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"persona not found: {persona_id}")
    return PersonaDetail(**detail.__dict__)


@router.post("", response_model=PersonaDetail, status_code=status.HTTP_201_CREATED)
async def create(body: PersonaCreate, _: User = Depends(current_user)) -> PersonaDetail:
    try:
        detail = personas_mod.create_persona(
            persona_id=body.id,
            name=body.name,
            description=body.description,
            soul=body.soul,
            memory=body.memory,
            style=body.style,
        )
    except personas_mod.PersonaError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return PersonaDetail(**detail.__dict__)


@router.put("/{persona_id}", response_model=PersonaDetail)
async def update(
    persona_id: str, body: PersonaUpdate, _: User = Depends(current_user)
) -> PersonaDetail:
    try:
        detail = personas_mod.update_persona(
            persona_id=persona_id,
            name=body.name,
            description=body.description,
            soul=body.soul,
            memory=body.memory,
            style=body.style,
        )
    except personas_mod.PersonaError as e:
        # 404 for "not found", 400 for malformed id.
        code = status.HTTP_404_NOT_FOUND if "not found" in str(e) else status.HTTP_400_BAD_REQUEST
        raise HTTPException(code, str(e)) from e
    return PersonaDetail(**detail.__dict__)


@router.delete("/{persona_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(persona_id: str, _: User = Depends(current_user)) -> None:
    try:
        personas_mod.delete_persona(persona_id)
    except personas_mod.PersonaError as e:
        msg = str(e)
        if "protected" in msg:
            raise HTTPException(status.HTTP_409_CONFLICT, msg) from e
        if "not found" in msg:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg) from e
        raise HTTPException(status.HTTP_400_BAD_REQUEST, msg) from e
