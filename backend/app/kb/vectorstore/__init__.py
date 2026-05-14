from __future__ import annotations

import logging

from app.core.config import settings

from .base import VectorHit, VectorStore
from .memory import MemoryVectorStore

__all__ = ["VectorHit", "VectorStore", "get_vector_store"]

log = logging.getLogger(__name__)
_store: VectorStore | None = None


def get_vector_store() -> VectorStore:
    """Resolve once per process.

    `VECTOR_STORE=milvus` makes Milvus a hard requirement: import / connect
    failures raise instead of silently dropping back to memory, so misconfig
    in prod is visible. `memory` (default) is always available.
    """
    global _store
    if _store is not None:
        return _store
    name = settings.vector_store.lower()
    if name == "milvus":
        from .milvus import MilvusVectorStore  # eager import so errors are visible

        _store = MilvusVectorStore(
            uri=settings.milvus_uri,
            collection=settings.milvus_collection,
            dim=settings.embedding_dim,
        )
        log.info("vector store: milvus uri=%s coll=%s", settings.milvus_uri, settings.milvus_collection)
        return _store
    _store = MemoryVectorStore()
    log.info("vector store: memory (in-process)")
    return _store


def reset_store() -> None:
    """Force re-resolution on next call (used by tests)."""
    global _store
    _store = None
