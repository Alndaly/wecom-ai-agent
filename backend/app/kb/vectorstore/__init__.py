from __future__ import annotations

from app.core.config import settings

from .base import VectorHit, VectorStore
from .memory import MemoryVectorStore

__all__ = ["VectorHit", "VectorStore", "get_vector_store"]

_store: VectorStore | None = None


def get_vector_store() -> VectorStore:
    global _store
    if _store is not None:
        return _store
    name = settings.vector_store.lower()
    if name == "milvus":
        # Stub; needs `pymilvus`. Falls back to memory if unavailable.
        try:
            from .milvus import MilvusVectorStore
            _store = MilvusVectorStore(
                uri=settings.milvus_uri,
                collection=settings.milvus_collection,
                dim=settings.embedding_dim,
            )
            return _store
        except Exception:  # noqa: BLE001
            pass
    _store = MemoryVectorStore()
    return _store
