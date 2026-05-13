from __future__ import annotations

import asyncio
from collections import defaultdict

from .base import Edge, GraphStore, Node


class MemoryGraphStore(GraphStore):
    name = "memory"

    def __init__(self) -> None:
        # team_id -> {(label, name): Node}
        self._nodes: dict[int, dict[tuple[str, str], Node]] = defaultdict(dict)
        # team_id -> { (label, name) : list[(rel, (label, name))] }
        self._adj: dict[int, dict[tuple[str, str], list[tuple[str, tuple[str, str]]]]] = (
            defaultdict(lambda: defaultdict(list))
        )
        self._lock = asyncio.Lock()

    async def upsert_node(self, team_id: int, node: Node) -> None:
        async with self._lock:
            self._nodes[team_id][node.key()] = node

    async def upsert_edge(self, team_id: int, edge: Edge) -> None:
        async with self._lock:
            self._nodes[team_id][edge.src.key()] = edge.src
            self._nodes[team_id][edge.dst.key()] = edge.dst
            adj = self._adj[team_id][edge.src.key()]
            pair = (edge.rel, edge.dst.key())
            if pair not in adj:
                adj.append(pair)

    async def neighbors(
        self, team_id: int, node: Node, *, hops: int = 1, limit: int = 20
    ) -> list[tuple[Node, str, Node]]:
        out: list[tuple[Node, str, Node]] = []
        seen: set[tuple[str, str]] = {node.key()}
        frontier: list[tuple[str, str]] = [node.key()]
        for _ in range(max(1, hops)):
            next_frontier: list[tuple[str, str]] = []
            for key in frontier:
                for rel, dst_key in self._adj[team_id].get(key, ()):
                    if len(out) >= limit:
                        return out
                    src_node = self._nodes[team_id].get(key)
                    dst_node = self._nodes[team_id].get(dst_key)
                    if not src_node or not dst_node:
                        continue
                    out.append((src_node, rel, dst_node))
                    if dst_key not in seen:
                        seen.add(dst_key)
                        next_frontier.append(dst_key)
            frontier = next_frontier
            if not frontier:
                break
        return out

    async def find_nodes(self, team_id: int, names: list[str]) -> list[Node]:
        lower = {n.lower() for n in names}
        return [
            n
            for (lbl, nm), n in self._nodes.get(team_id, {}).items()
            if nm in lower or any(t in nm for t in lower)
        ]
