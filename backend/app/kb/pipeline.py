"""Ingest pipeline: bytes → text → chunks → embeddings → vector & graph stores."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import KnowledgeBase, KnowledgeChunk, KnowledgeDocument

from . import chunker, entities, parsers
from .embeddings import get_embedding_provider
from .graphstore import Edge, Node, get_graph_store
from .vectorstore import get_vector_store

log = logging.getLogger(__name__)


async def ingest_document(db: AsyncSession, *, doc_id: int) -> None:
    doc = await db.get(KnowledgeDocument, doc_id)
    if doc is None:
        return
    kb = await db.get(KnowledgeBase, doc.kb_id)
    if kb is None:
        doc.status = "failed"
        doc.error = "kb not found"
        await db.commit()
        return

    doc.status = "processing"
    await db.commit()

    try:
        # the raw bytes are kept on the doc as a side table? For MVP3 we
        # stored bytes via the route before calling ingest. The route writes
        # the file content to a temp object; here we pull from disk-less path
        # by re-reading the chunks we already saved? Simpler: pipeline expects
        # the route to pass `_pending_bytes` via a class attribute.
        data = _PENDING.pop(doc_id, None)
        if data is None:
            raise RuntimeError("no pending bytes for doc")

        text = parsers.parse(doc.name, doc.mime, data)
        if not text.strip():
            raise RuntimeError("empty document after parsing")

        size = kb.chunk_size or settings.kb_chunk_size
        overlap = kb.chunk_overlap or settings.kb_chunk_overlap
        pieces = chunker.chunk(text, size=size, overlap=overlap)

        embedder = await get_embedding_provider(db, kb.team_id)
        vectors = await embedder.embed(pieces)

        seeds = [s.strip() for s in (kb.description or "").split(",") if s.strip()]
        vector_store = get_vector_store()
        graph_store = get_graph_store()

        ids_for_vec: list[str] = []
        vecs_for_vec: list[list[float]] = []
        metas_for_vec: list[dict] = []

        for ord_, (piece, vec) in enumerate(zip(pieces, vectors)):
            chunk_row = KnowledgeChunk(
                kb_id=kb.id,
                doc_id=doc.id,
                ord=ord_,
                text=piece,
                embedding_json=vec,
                entities_json=[],
            )
            db.add(chunk_row)
            await db.flush()  # assign chunk_row.id

            chunk_entities = entities.extract(piece, product_seeds=seeds)
            chunk_row.entities_json = [f"{lbl}:{nm}" for lbl, nm in chunk_entities]

            # graph: link chunk → entities, and pair entities together
            chunk_node = Node(label="Chunk", name=f"chunk-{chunk_row.id}")
            await graph_store.upsert_node(kb.team_id, chunk_node)
            ent_nodes = [Node(label=lbl, name=nm) for lbl, nm in chunk_entities]
            for n in ent_nodes:
                await graph_store.upsert_edge(
                    kb.team_id, Edge(src=chunk_node, dst=n, rel="MENTIONS")
                )
            # pair entities — coarse co-occurrence relation
            for i in range(len(ent_nodes)):
                for j in range(i + 1, len(ent_nodes)):
                    await graph_store.upsert_edge(
                        kb.team_id, Edge(src=ent_nodes[i], dst=ent_nodes[j], rel="CO_OCCURS")
                    )

            embedding_id = f"chunk-{chunk_row.id}"
            chunk_row.embedding_id = embedding_id
            ids_for_vec.append(embedding_id)
            vecs_for_vec.append(vec)
            metas_for_vec.append(
                {
                    "team_id": kb.team_id,
                    "kb_id": kb.id,
                    "doc_id": doc.id,
                    "chunk_id": chunk_row.id,
                    "text": piece,
                }
            )

        await vector_store.upsert(ids_for_vec, vecs_for_vec, metas_for_vec)

        doc.chunk_count = len(pieces)
        doc.status = "ready"
        doc.error = None
        await db.commit()
        log.info("ingested doc id=%s chunks=%s", doc.id, len(pieces))
    except Exception as e:  # noqa: BLE001
        log.exception("ingest failed for doc %s", doc.id)
        doc.status = "failed"
        doc.error = str(e)
        await db.commit()


# ---- staging area: the route stashes raw bytes here, ingest pops them.
# Avoids putting bytes on the SQLAlchemy row and keeps the function call
# signature small.
_PENDING: dict[int, bytes] = {}


def stash_bytes(doc_id: int, data: bytes) -> None:
    _PENDING[doc_id] = data
