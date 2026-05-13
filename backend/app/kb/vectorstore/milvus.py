"""Milvus adapter — opt-in, imported lazily so the system runs without pymilvus.

Schema (per-team is enforced at query-time via meta filter):
  id          INT64        primary
  vector      FLOAT_VECTOR dim
  team_id     INT64
  kb_id       INT64
  doc_id      INT64
  chunk_id    INT64
  text        VARCHAR      (max 4000)

MVP3 ships the adapter shape and falls back to MemoryVectorStore by default —
flip on by setting VECTOR_STORE=milvus + MILVUS_URI.
"""
from __future__ import annotations

from .base import VectorHit, VectorStore


class MilvusVectorStore(VectorStore):
    name = "milvus"

    def __init__(self, *, uri: str, collection: str, dim: int) -> None:
        # importing here avoids a hard dependency
        from pymilvus import MilvusClient  # type: ignore

        self.client = MilvusClient(uri=uri)
        self.collection = collection
        self.dim = dim
        if not self.client.has_collection(collection):
            self.client.create_collection(
                collection_name=collection,
                dimension=dim,
                metric_type="COSINE",
                auto_id=True,
            )

    async def upsert(self, ids, vectors, metas) -> None:
        rows = [
            {"vector": v, "text": m.get("text", ""), **{k: m.get(k) for k in ("team_id", "kb_id", "doc_id", "chunk_id")}}
            for v, m in zip(vectors, metas)
        ]
        self.client.insert(self.collection, rows)

    async def search(self, vector, *, top_k=5, filter_=None) -> list[VectorHit]:
        expr = None
        if filter_:
            expr = " and ".join(f"{k} == {v!r}" if isinstance(v, str) else f"{k} == {v}" for k, v in filter_.items())
        res = self.client.search(
            collection_name=self.collection,
            data=[vector],
            limit=top_k,
            filter=expr,
            output_fields=["team_id", "kb_id", "doc_id", "chunk_id", "text"],
        )
        out: list[VectorHit] = []
        for hits in res:
            for h in hits:
                out.append(VectorHit(id=str(h["id"]), score=float(h["distance"]), meta=h.get("entity", {})))
        return out

    async def delete_by_meta(self, key: str, value) -> None:
        expr = f"{key} == {value!r}" if isinstance(value, str) else f"{key} == {value}"
        self.client.delete(self.collection, filter=expr)
