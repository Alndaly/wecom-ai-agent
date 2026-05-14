"""Runtime settings — team-scoped, hot-reloadable from the Web UI.

Scopes:
  - llm        provider / model / api_key / base_url / temperature
  - embedding  provider / model / api_key / base_url / dim
  - retrieval  top_k / min_score
  - ai         confidence_threshold / context_window / default_prompt

`api_key` is write-only: GET returns a masked placeholder so we never leak
secrets back to the browser; PUT only updates the key if a non-empty value
is provided.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.providers import build_provider, reset_cache as reset_llm_cache
from app.ai.providers.base import ChatMessage
from app.core.db import get_db
from app.deps import current_user
from app.kb.embeddings import (
    build_embedding_provider,
    reset_cache as reset_embedding_cache,
)
from app.kb.vectorstore import get_vector_store
from app.kb.graphstore import get_graph_store
from app.core.config import settings as env_settings
from app.models import User
from app.services import settings_service

router = APIRouter(prefix="/settings", tags=["settings"])

Scope = Literal["llm", "embedding", "retrieval", "ai"]


# ---------- masking ----------
_MASK = "********"


def _mask_api_key(d: dict) -> dict:
    out = dict(d)
    if "api_key" in out:
        out["api_key"] = _MASK if out["api_key"] else ""
    return out


# ---------- schemas ----------
class LLMIn(BaseModel):
    provider: Literal["mock", "openai"] = "openai"
    model: str = Field(default="gpt-4o-mini", max_length=128)
    # Empty string means "do not change" on PUT.
    api_key: str = ""
    base_url: str = ""
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)


class EmbeddingIn(BaseModel):
    provider: Literal["mock", "openai"] = "openai"
    model: str = Field(default="text-embedding-3-small", max_length=128)
    api_key: str = ""
    base_url: str = ""
    dim: int = Field(default=1536, ge=16, le=8192)


class RetrievalIn(BaseModel):
    top_k: int = Field(default=5, ge=1, le=50)
    min_score: float = Field(default=0.25, ge=0.0, le=1.0)


class AIBehaviorIn(BaseModel):
    confidence_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    context_window: int = Field(default=10, ge=1, le=50)
    default_prompt: str = ""


# ---------- read all ----------
@router.get("")
async def read_all(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    out = {}
    for scope in ("llm", "embedding", "retrieval", "ai"):
        v = await settings_service.get(db, user.team_id, scope)
        if scope in ("llm", "embedding"):
            v = _mask_api_key(v)
        out[scope] = v
    # also surface the read-only infra config so the UI can show it
    out["infra"] = {
        "vector_store": env_settings.vector_store,
        "graph_store": env_settings.graph_store,
        "milvus_uri": env_settings.milvus_uri,
        "milvus_collection": env_settings.milvus_collection,
        "neo4j_uri": env_settings.neo4j_uri,
    }
    return out


# ---------- write per-scope ----------
async def _upsert(
    db: AsyncSession,
    team_id: int,
    scope: Scope,
    payload: dict,
    *,
    treat_empty_api_key_as_keep: bool = True,
) -> int:
    # if api_key blank, drop it so the existing one is preserved
    if treat_empty_api_key_as_keep and "api_key" in payload and not payload["api_key"]:
        payload = {k: v for k, v in payload.items() if k != "api_key"}
    return await settings_service.upsert(db, team_id, scope, payload)


@router.put("/llm")
async def write_llm(
    body: LLMIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    ver = await _upsert(db, user.team_id, "llm", body.model_dump())
    reset_llm_cache(user.team_id)
    return {"version": ver}


@router.put("/embedding")
async def write_embedding(
    body: EmbeddingIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    ver = await _upsert(db, user.team_id, "embedding", body.model_dump())
    reset_embedding_cache(user.team_id)
    return {"version": ver}


@router.put("/retrieval")
async def write_retrieval(
    body: RetrievalIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    ver = await _upsert(db, user.team_id, "retrieval", body.model_dump())
    return {"version": ver}


@router.put("/ai")
async def write_ai_behavior(
    body: AIBehaviorIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ver = await _upsert(db, user.team_id, "ai", body.model_dump())
    return {"version": ver}


# ---------- probes ----------
class ProbeOut(BaseModel):
    ok: bool
    detail: str
    latency_ms: int | None = None
    model: str | None = None
    dim: int | None = None


@router.post("/test/llm", response_model=ProbeOut)
async def test_llm(
    body: LLMIn | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> ProbeOut:
    """One-shot ping. If a body is provided, the form values are tested
    *without* persisting; otherwise the currently-saved config is used.
    api_key="" in the body means "use saved value".
    """
    saved = await settings_service.get(db, user.team_id, "llm")
    cfg = dict(saved)
    if body is not None:
        payload = body.model_dump()
        if not payload.get("api_key"):
            payload.pop("api_key", None)
        cfg.update(payload)
    try:
        provider = build_provider(cfg)
        result = await provider.chat(
            [ChatMessage(role="user", content="ping")],
            temperature=0.0,
            max_tokens=16,
        )
        return ProbeOut(
            ok=True,
            detail=result.text[:80] or "(empty)",
            latency_ms=result.latency_ms,
            model=result.model,
        )
    except Exception as e:  # noqa: BLE001
        return ProbeOut(ok=False, detail=str(e)[:300])


@router.post("/test/embedding", response_model=ProbeOut)
async def test_embedding(
    body: EmbeddingIn | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> ProbeOut:
    saved = await settings_service.get(db, user.team_id, "embedding")
    cfg = dict(saved)
    if body is not None:
        payload = body.model_dump()
        if not payload.get("api_key"):
            payload.pop("api_key", None)
        cfg.update(payload)
    try:
        provider = build_embedding_provider(cfg)
        vec = await provider.embed_one("ping")
        return ProbeOut(
            ok=True,
            detail=f"vector returned, |v|={len(vec)}",
            model=getattr(provider, "model", provider.name),
            dim=len(vec),
        )
    except Exception as e:  # noqa: BLE001
        return ProbeOut(ok=False, detail=str(e)[:300])


@router.post("/test/vector_store", response_model=ProbeOut)
async def test_vector_store(user: User = Depends(current_user)) -> ProbeOut:
    try:
        store = get_vector_store()
        # round-trip a single zero vector under a sentinel meta
        dim = 8
        await store.upsert(
            ["__probe__"],
            [[0.0] * dim],
            [{"team_id": -1, "kb_id": -1, "doc_id": -1, "chunk_id": -1, "text": "probe"}],
        )
        await store.delete_by_meta("team_id", -1)
        return ProbeOut(ok=True, detail=f"backend={store.name}")
    except Exception as e:  # noqa: BLE001
        return ProbeOut(ok=False, detail=str(e)[:300])


@router.post("/test/graph_store", response_model=ProbeOut)
async def test_graph_store(user: User = Depends(current_user)) -> ProbeOut:
    try:
        store = get_graph_store()
        return ProbeOut(ok=True, detail=f"backend={store.name}")
    except Exception as e:  # noqa: BLE001
        return ProbeOut(ok=False, detail=str(e)[:300])
