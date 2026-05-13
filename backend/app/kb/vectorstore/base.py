from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class VectorHit:
    id: str               # provider-side id (returned as embedding_id)
    score: float          # cosine similarity [-1, 1]; higher = closer
    meta: dict            # e.g. {"team_id":..,"kb_id":..,"chunk_id":..,"doc_id":..}


class VectorStore(ABC):
    name: str

    @abstractmethod
    async def upsert(
        self,
        ids: list[str],
        vectors: list[list[float]],
        metas: list[dict],
    ) -> None: ...

    @abstractmethod
    async def search(
        self,
        vector: list[float],
        *,
        top_k: int = 5,
        filter_: dict | None = None,
    ) -> list[VectorHit]: ...

    @abstractmethod
    async def delete_by_meta(self, key: str, value) -> None: ...
