"""Hybrid retrieval: vector top-K + 1-hop graph expansion.

Per-team embedding provider + per-team retrieval settings.

Public surface:
  retrieve(db, team_id, query, top_k=None) -> RetrievalResult
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import KnowledgeChunk
from app.services import settings_service

from .embeddings import get_embedding_provider
from .graphstore import Node, get_graph_store
from .vectorstore import get_vector_store


@dataclass
class Hit:
    chunk_id: int
    doc_id: int
    kb_id: int
    text: str
    score: float


@dataclass
class GraphFact:
    src_label: str
    src_name: str
    rel: str
    dst_label: str
    dst_name: str


@dataclass
class RetrievalResult:
    hits: list[Hit] = field(default_factory=list)
    graph_facts: list[GraphFact] = field(default_factory=list)

    def to_context(self) -> str:
        if not self.hits and not self.graph_facts:
            return ""
        parts: list[str] = []
        if self.hits:
            parts.append("【知识库片段】")
            for i, h in enumerate(self.hits, 1):
                parts.append(f"[{i}] (score={h.score:.2f}) {h.text}")
        if self.graph_facts:
            parts.append("【关联实体】")
            for f in self.graph_facts:
                parts.append(f"- ({f.src_label}:{f.src_name}) -[{f.rel}]-> ({f.dst_label}:{f.dst_name})")
        return "\n".join(parts)


async def retrieve(
    db: AsyncSession,
    *,
    team_id: int,
    query: str,
    top_k: int | None = None,
    expand_graph: bool = True,
) -> RetrievalResult:
    query = (query or "").strip()
    if not query:
        return RetrievalResult()

    retrieval_cfg = await settings_service.get(db, team_id, "retrieval")
    k = top_k or int(retrieval_cfg.get("top_k") or settings.kb_top_k)
    floor = float(retrieval_cfg.get("min_score") or settings.kb_min_score)

    embedder = await get_embedding_provider(db, team_id)
    embed = await embedder.embed_one(query)
    vector_hits = await get_vector_store().search(
        embed, top_k=k, filter_={"team_id": team_id}
    )

    keep = [h for h in vector_hits if h.score >= floor]

    hits: list[Hit] = []
    chunk_ids: list[int] = []
    for vh in keep:
        cid = vh.meta.get("chunk_id")
        if cid is None:
            continue
        chunk_ids.append(int(cid))
        hits.append(
            Hit(
                chunk_id=int(cid),
                doc_id=int(vh.meta.get("doc_id") or 0),
                kb_id=int(vh.meta.get("kb_id") or 0),
                text=str(vh.meta.get("text") or ""),
                score=vh.score,
            )
        )

    if hits and any(not h.text for h in hits):
        rows = (
            await db.execute(
                select(KnowledgeChunk).where(KnowledgeChunk.id.in_(chunk_ids))
            )
        ).scalars().all()
        by_id = {r.id: r for r in rows}
        for h in hits:
            if not h.text and h.chunk_id in by_id:
                h.text = by_id[h.chunk_id].text

    graph_facts: list[GraphFact] = []
    if expand_graph and chunk_ids:
        first = chunk_ids[0]
        row = await db.get(KnowledgeChunk, first)
        if row and row.entities_json:
            seeds = []
            for s in row.entities_json:
                if ":" in s:
                    lbl, nm = s.split(":", 1)
                    seeds.append(Node(label=lbl, name=nm))
            store = get_graph_store()
            for n in seeds[:5]:
                neigh = await store.neighbors(team_id, n, hops=1, limit=8)
                for src, rel, dst in neigh:
                    if src.label == "Chunk" or dst.label == "Chunk":
                        continue
                    graph_facts.append(
                        GraphFact(
                            src_label=src.label,
                            src_name=src.name,
                            rel=rel,
                            dst_label=dst.label,
                            dst_name=dst.name,
                        )
                    )
        seen: set[tuple] = set()
        deduped: list[GraphFact] = []
        for f in graph_facts:
            key = (f.src_label, f.src_name, f.rel, f.dst_label, f.dst_name)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(f)
        graph_facts = deduped[:10]

    return RetrievalResult(hits=hits, graph_facts=graph_facts)
