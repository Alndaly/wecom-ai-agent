from __future__ import annotations

from app.core.config import settings

from .base import Edge, GraphStore, Node
from .memory import MemoryGraphStore

__all__ = ["Edge", "GraphStore", "Node", "get_graph_store"]

_store: GraphStore | None = None


def get_graph_store() -> GraphStore:
    global _store
    if _store is not None:
        return _store
    if settings.graph_store.lower() == "neo4j":
        try:
            from .neo4j_store import Neo4jGraphStore
            _store = Neo4jGraphStore(
                uri=settings.neo4j_uri,
                user=settings.neo4j_user,
                password=settings.neo4j_password,
            )
            return _store
        except Exception:
            pass
    _store = MemoryGraphStore()
    return _store
