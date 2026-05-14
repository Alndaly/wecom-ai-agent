"""Milvus adapter — opt-in.

Schema (multi-tenant enforced via scalar filter at query-time):
  id          INT64        primary, auto_id
  vector      FLOAT_VECTOR dim
  team_id     INT64
  kb_id       INT64
  doc_id      INT64
  chunk_id    INT64
  text        VARCHAR(8000)

Selected by env VECTOR_STORE=milvus; falls back to memory if pymilvus is
absent or the server is unreachable.
"""
from __future__ import annotations

import asyncio
import logging

from .base import VectorHit, VectorStore

log = logging.getLogger(__name__)


class MilvusVectorStore(VectorStore):
    name = "milvus"

    def __init__(self, *, uri: str, collection: str, dim: int) -> None:
        from pymilvus import DataType, MilvusClient  # type: ignore

        self.MilvusClient = MilvusClient
        self.DataType = DataType
        self.client = MilvusClient(uri=uri)
        self.collection = collection
        self.dim = dim
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        DataType = self.DataType
        if self.client.has_collection(self.collection):
            return
        schema = self.client.create_schema(auto_id=True, enable_dynamic_field=False)
        schema.add_field("id", DataType.INT64, is_primary=True)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.dim)
        schema.add_field("team_id", DataType.INT64)
        schema.add_field("kb_id", DataType.INT64)
        schema.add_field("doc_id", DataType.INT64)
        schema.add_field("chunk_id", DataType.INT64)
        schema.add_field("text", DataType.VARCHAR, max_length=8000)

        index_params = self.client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 16, "efConstruction": 200},
        )
        self.client.create_collection(
            collection_name=self.collection,
            schema=schema,
            index_params=index_params,
        )
        self.client.load_collection(self.collection)
        log.info("milvus: created collection %s dim=%s", self.collection, self.dim)

    async def upsert(self, ids, vectors, metas) -> None:
        rows = [
            {
                "vector": v,
                "team_id": int(m.get("team_id") or 0),
                "kb_id": int(m.get("kb_id") or 0),
                "doc_id": int(m.get("doc_id") or 0),
                "chunk_id": int(m.get("chunk_id") or 0),
                "text": (m.get("text") or "")[:7990],
            }
            for v, m in zip(vectors, metas)
        ]
        await asyncio.to_thread(self.client.insert, self.collection, rows)

    async def search(self, vector, *, top_k=5, filter_=None) -> list[VectorHit]:
        expr = None
        if filter_:
            parts: list[str] = []
            for k, v in filter_.items():
                parts.append(f"{k} == {v!r}" if isinstance(v, str) else f"{k} == {v}")
            expr = " and ".join(parts)
        res = await asyncio.to_thread(
            self.client.search,
            collection_name=self.collection,
            data=[vector],
            limit=top_k,
            filter=expr,
            output_fields=["team_id", "kb_id", "doc_id", "chunk_id", "text"],
            search_params={"metric_type": "COSINE", "params": {"ef": 64}},
        )
        out: list[VectorHit] = []
        for hits in res:
            for h in hits:
                entity = h.get("entity") if isinstance(h, dict) else None
                meta = entity or {k: h.get(k) for k in ("team_id", "kb_id", "doc_id", "chunk_id", "text")}
                # Milvus returns COSINE in [0, 2]; convert to similarity in [-1, 1]
                # Newer pymilvus already returns similarity for COSINE; treat 'distance' as score.
                score = float(h.get("distance", 0.0) if isinstance(h, dict) else 0.0)
                out.append(VectorHit(id=str(h.get("id")), score=score, meta=meta))
        return out

    async def delete_by_meta(self, key: str, value) -> None:
        expr = f"{key} == {value!r}" if isinstance(value, str) else f"{key} == {value}"
        await asyncio.to_thread(self.client.delete, self.collection, filter=expr)
