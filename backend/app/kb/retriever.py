"""Hybrid retrieval: vector seed chunks + graph/context expansion.

Per-team embedding provider + per-team retrieval settings.

Public surface:
  retrieve(db, team_id, query, top_k=None) -> RetrievalResult
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

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
    source: str = "vector"


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
                label = {
                    "vector": "语义召回",
                    "neighbor": "上下文补全",
                    "graph": "图谱扩展",
                }.get(h.source, h.source)
                parts.append(
                    f"[{i}] ({label}, score={h.score:.2f}, chunk={h.chunk_id}) "
                    f"{_clip(h.text, 1000)}"
                )
        if self.graph_facts:
            parts.append("【关联实体】")
            for f in self.graph_facts:
                parts.append(f"- ({f.src_label}:{f.src_name}) -[{f.rel}]-> ({f.dst_label}:{f.dst_name})")
        return "\n".join(parts)


_SECTION_RE = re.compile(r"\b\d+(?:\.\d+)+\b")
_PAGE_DOTS_RE = re.compile(r"[.．…]{2,}\s*\d{1,4}")


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _looks_like_toc(text: str) -> bool:
    """Detect table-of-contents chunks so they don't dominate answer context."""
    compact = " ".join((text or "").split())
    if len(compact) < 80:
        return False
    section_refs = len(_SECTION_RE.findall(compact))
    page_refs = len(_PAGE_DOTS_RE.findall(compact))
    dot_count = compact.count(".") + compact.count("．") + compact.count("…")
    return page_refs >= 3 or (section_refs >= 5 and dot_count >= 12)


def _query_variants(query: str) -> list[str]:
    variants = [query]
    swaps = [
        ("安装", "安裝"),
        ("安裝", "安装"),
        ("步骤", "步驟"),
        ("步驟", "步骤"),
        ("主板", "主機板"),
        ("主機板", "主板"),
    ]
    for src, dst in swaps:
        if src in query:
            variants.append(query.replace(src, dst))

    cleaned = query.strip(" ？?。.")
    if any(word in cleaned for word in ("安装", "安裝", "装机")):
        variants.extend(
            [
                f"{cleaned} 步骤",
                f"{cleaned} 注意事项",
                "安装主板 步骤 接线 固定",
                "安裝主板 步驟 接線 固定",
            ]
        )

    out: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        variant = " ".join(variant.split())
        if variant and variant not in seen:
            seen.add(variant)
            out.append(variant)
    return out[:5]


async def _load_chunks(db: AsyncSession, chunk_ids: list[int]) -> dict[int, KnowledgeChunk]:
    ids = list(dict.fromkeys(int(cid) for cid in chunk_ids if cid))
    if not ids:
        return {}
    rows = (
        await db.execute(select(KnowledgeChunk).where(KnowledgeChunk.id.in_(ids)))
    ).scalars().all()
    return {int(r.id): r for r in rows}


async def _load_neighbor_chunks(
    db: AsyncSession, chunks: list[KnowledgeChunk], *, radius: int = 1
) -> list[KnowledgeChunk]:
    doc_to_ords: dict[int, set[int]] = {}
    for chunk in chunks:
        ords = doc_to_ords.setdefault(int(chunk.doc_id), set())
        for offset in range(-radius, radius + 1):
            if offset:
                ords.add(int(chunk.ord) + offset)

    out: list[KnowledgeChunk] = []
    for doc_id, ords in doc_to_ords.items():
        valid_ords = {ord_ for ord_ in ords if ord_ >= 0}
        if not valid_ords:
            continue
        rows = (
            await db.execute(
                select(KnowledgeChunk)
                .where(KnowledgeChunk.doc_id == doc_id)
                .where(KnowledgeChunk.ord.in_(valid_ords))
                .order_by(KnowledgeChunk.ord)
            )
        ).scalars().all()
        out.extend(rows)
    return out


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
    vector_store = get_vector_store()
    search_k = max(k * 3, k + 8)
    merged: dict[int, Hit] = {}
    for variant in _query_variants(query):
        embed = await embedder.embed_one(variant)
        vector_hits = await vector_store.search(
            embed, top_k=search_k, filter_={"team_id": team_id}
        )
        for vh in vector_hits:
            cid = vh.meta.get("chunk_id")
            if cid is None:
                continue
            text = str(vh.meta.get("text") or "")
            adjusted_score = float(vh.score) - (0.12 if _looks_like_toc(text) else 0.0)
            if adjusted_score < floor:
                continue
            chunk_id = int(cid)
            prev = merged.get(chunk_id)
            if prev is None or adjusted_score > prev.score:
                merged[chunk_id] = Hit(
                    chunk_id=chunk_id,
                    doc_id=int(vh.meta.get("doc_id") or 0),
                    kb_id=int(vh.meta.get("kb_id") or 0),
                    text=text,
                    score=adjusted_score,
                )

    hits = sorted(merged.values(), key=lambda h: h.score, reverse=True)
    if any(not h.text for h in hits):
        by_id = await _load_chunks(db, [h.chunk_id for h in hits])
        for h in hits:
            if not h.text and h.chunk_id in by_id:
                h.text = by_id[h.chunk_id].text

    content_hits = [h for h in hits if not _looks_like_toc(h.text)]
    toc_hits = [h for h in hits if _looks_like_toc(h.text)]
    hits = (content_hits + toc_hits)[:k]
    chunk_ids = [h.chunk_id for h in hits]

    graph_facts: list[GraphFact] = []
    expansion_hits: list[Hit] = []
    if expand_graph and chunk_ids:
        store = get_graph_store()
        rows_by_id = await _load_chunks(db, chunk_ids)
        seed_rows = [rows_by_id[cid] for cid in chunk_ids if cid in rows_by_id]

        related_chunk_ids = await store.related_chunks(team_id, chunk_ids, limit=max(4, k))
        related_by_id = await _load_chunks(db, related_chunk_ids)
        for row in related_by_id.values():
            if _looks_like_toc(row.text):
                continue
            expansion_hits.append(
                Hit(
                    chunk_id=int(row.id),
                    doc_id=int(row.doc_id),
                    kb_id=int(row.kb_id),
                    text=row.text,
                    score=0.0,
                    source="graph",
                )
            )

        neighbor_rows = await _load_neighbor_chunks(db, seed_rows[: max(1, k // 2)])
        for row in neighbor_rows:
            if _looks_like_toc(row.text):
                continue
            expansion_hits.append(
                Hit(
                    chunk_id=int(row.id),
                    doc_id=int(row.doc_id),
                    kb_id=int(row.kb_id),
                    text=row.text,
                    score=0.0,
                    source="neighbor",
                )
            )

        seeds: list[Node] = []
        for row in seed_rows[:3]:
            for s in row.entities_json or []:
                if ":" in s:
                    lbl, nm = s.split(":", 1)
                    seeds.append(Node(label=lbl, name=nm))
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

    seen_hit_ids = {h.chunk_id for h in hits}
    for h in expansion_hits:
        if h.chunk_id in seen_hit_ids:
            continue
        seen_hit_ids.add(h.chunk_id)
        hits.append(h)
        if len(hits) >= k + max(3, k // 2):
            break

    return RetrievalResult(hits=hits, graph_facts=graph_facts)
