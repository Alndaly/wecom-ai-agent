from __future__ import annotations

from datetime import datetime

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete as sa_delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal, get_db
from app.deps import current_user
from app.kb import pipeline
from app.kb.graphstore import get_graph_store
from app.kb.retriever import retrieve
from app.kb.vectorstore import get_vector_store
from app.models import KnowledgeBase, KnowledgeChunk, KnowledgeDocument, User

router = APIRouter(prefix="/kb", tags=["knowledge"])


# --- schemas ---
class KBIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    chunk_size: int = Field(400, ge=100, le=2000)
    chunk_overlap: int = Field(60, ge=0, le=500)


class KBOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str
    chunk_size: int
    chunk_overlap: int
    version: int
    created_at: datetime


class DocOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    source: str
    mime: str
    status: str
    chunk_count: int
    bytes: int
    error: str | None
    created_at: datetime
    updated_at: datetime


class SearchIn(BaseModel):
    query: str
    top_k: int = Field(5, ge=1, le=20)


class SearchHitOut(BaseModel):
    chunk_id: int
    doc_id: int
    kb_id: int
    text: str
    score: float


class SearchOut(BaseModel):
    hits: list[SearchHitOut]
    graph_facts: list[dict]


# --- knowledge bases ---
@router.get("", response_model=list[KBOut])
async def list_kb(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(KnowledgeBase).where(KnowledgeBase.team_id == user.team_id).order_by(KnowledgeBase.id)
        )
    ).scalars().all()
    return list(rows)


@router.post("", response_model=KBOut, status_code=status.HTTP_201_CREATED)
async def create_kb(
    body: KBIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = KnowledgeBase(
        team_id=user.team_id,
        name=body.name,
        description=body.description,
        chunk_size=body.chunk_size,
        chunk_overlap=body.chunk_overlap,
    )
    db.add(kb)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "kb name conflict") from e
    await db.refresh(kb)
    return kb


async def _get_kb(db: AsyncSession, kb_id: int, team_id: int) -> KnowledgeBase:
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.team_id != team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "kb not found")
    return kb


@router.get("/{kb_id}", response_model=KBOut)
async def get_kb(kb_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await _get_kb(db, kb_id, user.team_id)


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kb(kb_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    kb = await _get_kb(db, kb_id, user.team_id)
    chunk_ids = (
        await db.execute(select(KnowledgeChunk.id).where(KnowledgeChunk.kb_id == kb.id))
    ).scalars().all()
    vs = get_vector_store()
    gs = get_graph_store()
    await vs.delete_by_meta("kb_id", kb.id)
    await gs.delete_chunks(user.team_id, list(chunk_ids))
    await db.execute(sa_delete(KnowledgeChunk).where(KnowledgeChunk.kb_id == kb.id))
    await db.execute(sa_delete(KnowledgeDocument).where(KnowledgeDocument.kb_id == kb.id))
    await db.delete(kb)
    await db.commit()


# --- documents ---
@router.get("/{kb_id}/docs", response_model=list[DocOut])
async def list_docs(
    kb_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    await _get_kb(db, kb_id, user.team_id)
    rows = (
        await db.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.kb_id == kb_id).order_by(desc(KnowledgeDocument.id))
        )
    ).scalars().all()
    return list(rows)


@router.post("/{kb_id}/docs", response_model=DocOut, status_code=status.HTTP_201_CREATED)
async def upload_doc(
    kb_id: int,
    bg: BackgroundTasks,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_kb(db, kb_id, user.team_id)
    data = await file.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty file")

    doc = KnowledgeDocument(
        kb_id=kb.id,
        name=file.filename or "untitled",
        mime=file.content_type or "application/octet-stream",
        bytes=len(data),
        status="pending",
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    pipeline.stash_bytes(doc.id, data)

    async def _run() -> None:
        async with SessionLocal() as bdb:
            await pipeline.ingest_document(bdb, doc_id=doc.id)

    bg.add_task(_run)
    return doc


@router.post("/{kb_id}/docs/paste", response_model=DocOut, status_code=status.HTTP_201_CREATED)
async def paste_doc(
    kb_id: int,
    bg: BackgroundTasks,
    name: str = Form(...),
    content: str = Form(...),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Paste raw text — convenient for quick demos and tests."""
    kb = await _get_kb(db, kb_id, user.team_id)
    data = content.encode("utf-8")
    doc = KnowledgeDocument(
        kb_id=kb.id,
        name=name,
        source="paste",
        mime="text/plain",
        bytes=len(data),
        status="pending",
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    pipeline.stash_bytes(doc.id, data)

    async def _run() -> None:
        async with SessionLocal() as bdb:
            await pipeline.ingest_document(bdb, doc_id=doc.id)

    bg.add_task(_run)
    return doc


@router.delete("/{kb_id}/docs/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_doc(
    kb_id: int,
    doc_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_kb(db, kb_id, user.team_id)
    doc = await db.get(KnowledgeDocument, doc_id)
    if not doc or doc.kb_id != kb_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "doc not found")
    chunk_ids = (
        await db.execute(select(KnowledgeChunk.id).where(KnowledgeChunk.doc_id == doc.id))
    ).scalars().all()
    vs = get_vector_store()
    gs = get_graph_store()
    await vs.delete_by_meta("doc_id", doc.id)
    await gs.delete_chunks(user.team_id, list(chunk_ids))
    await db.execute(sa_delete(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc.id))
    await db.delete(doc)
    await db.commit()


@router.get("/{kb_id}/docs/{doc_id}", response_model=DocOut)
async def get_doc(
    kb_id: int,
    doc_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_kb(db, kb_id, user.team_id)
    doc = await db.get(KnowledgeDocument, doc_id)
    if not doc or doc.kb_id != kb_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "doc not found")
    return doc


# --- chunk lookup (used by Web right-panel KB hits) ---
class ChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    doc_id: int
    kb_id: int
    ord: int
    text: str


@router.get("/chunks/by-ids", response_model=list[ChunkOut])
async def chunks_by_ids(
    ids: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        id_list = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad ids")
    if not id_list:
        return []
    rows = (
        await db.execute(
            select(KnowledgeChunk)
            .join(KnowledgeBase, KnowledgeBase.id == KnowledgeChunk.kb_id)
            .where(KnowledgeChunk.id.in_(id_list), KnowledgeBase.team_id == user.team_id)
        )
    ).scalars().all()
    return list(rows)


# --- search ---
@router.post("/search", response_model=SearchOut)
async def search(
    body: SearchIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await retrieve(db, team_id=user.team_id, query=body.query, top_k=body.top_k)
    return SearchOut(
        hits=[
            SearchHitOut(
                chunk_id=h.chunk_id,
                doc_id=h.doc_id,
                kb_id=h.kb_id,
                text=h.text,
                score=h.score,
            )
            for h in res.hits
        ],
        graph_facts=[
            {
                "src": f"{f.src_label}:{f.src_name}",
                "rel": f.rel,
                "dst": f"{f.dst_label}:{f.dst_name}",
            }
            for f in res.graph_facts
        ],
    )
