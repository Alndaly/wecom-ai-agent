"""In-process vector store.

Linear scan with cosine similarity. Fine for hundreds of thousands of chunks
on one node; swap to Milvus for production scale via env LLM/embedding config
(see __init__.py).
"""
from __future__ import annotations

import asyncio
import math
import uuid

from .base import VectorHit, VectorStore


class MemoryVectorStore(VectorStore):
    name = "memory"

    def __init__(self) -> None:
        # id -> (vector, meta)
        self._data: dict[str, tuple[list[float], dict]] = {}
        self._lock = asyncio.Lock()

    async def upsert(
        self,
        ids: list[str],
        vectors: list[list[float]],
        metas: list[dict],
    ) -> None:
        assert len(ids) == len(vectors) == len(metas)
        async with self._lock:
            for i, v, m in zip(ids, vectors, metas):
                if not i:
                    i = uuid.uuid4().hex
                self._data[i] = (v, m)

    async def search(
        self,
        vector: list[float],
        *,
        top_k: int = 5,
        filter_: dict | None = None,
    ) -> list[VectorHit]:
        if not self._data:
            return []
        items = self._data.items()
        if filter_:
            items = [
                (i, (v, m))
                for i, (v, m) in items
                if all(m.get(k) == val for k, val in filter_.items())
            ]
        scored: list[tuple[float, str, dict]] = []
        for i, (v, m) in items:
            scored.append((_cosine(vector, v), i, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [VectorHit(id=i, score=s, meta=m) for s, i, m in scored[:top_k]]

    async def delete_by_meta(self, key: str, value) -> None:
        async with self._lock:
            for i in [i for i, (_, m) in self._data.items() if m.get(key) == value]:
                self._data.pop(i, None)


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return -1.0
    s = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        s += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return -1.0
    return s / (math.sqrt(na) * math.sqrt(nb))
