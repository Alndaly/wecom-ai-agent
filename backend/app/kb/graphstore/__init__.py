from __future__ import annotations

import logging

from app.core.config import settings

from .base import Edge, GraphStore, Node
from .memory import MemoryGraphStore

__all__ = ["Edge", "GraphStore", "Node", "get_graph_store", "reset_store"]

log = logging.getLogger(__name__)
_store: GraphStore | None = None


def get_graph_store() -> GraphStore:
    global _store
    if _store is not None:
        return _store
    if settings.graph_store.lower() == "neo4j":
        from .neo4j_store import Neo4jGraphStore  # raise on import / connect

        _store = Neo4jGraphStore(
            uri=settings.neo4j_uri,
            user=settings.neo4j_user,
            password=settings.neo4j_password,
        )
        log.info("graph store: neo4j uri=%s", settings.neo4j_uri)
        return _store
    _store = MemoryGraphStore()
    log.info("graph store: memory (in-process)")
    return _store


def reset_store() -> None:
    global _store
    _store = None
