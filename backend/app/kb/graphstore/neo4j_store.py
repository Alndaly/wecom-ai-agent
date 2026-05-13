"""Neo4j adapter — opt-in. Requires the official `neo4j` driver.

Schema (multi-tenant via property):
  (:Entity {team_id, label, name})
  edges: typed by `rel` value

Adjustments may be made later; for MVP3 we keep it generic.
"""
from __future__ import annotations

from .base import Edge, GraphStore, Node


class Neo4jGraphStore(GraphStore):
    name = "neo4j"

    def __init__(self, *, uri: str, user: str, password: str) -> None:
        from neo4j import AsyncGraphDatabase  # type: ignore

        self.driver = AsyncGraphDatabase.driver(uri, auth=(user, password))

    async def upsert_node(self, team_id: int, node: Node) -> None:
        async with self.driver.session() as s:
            await s.run(
                "MERGE (n:Entity {team_id:$tid, label:$lbl, name:$nm})",
                tid=team_id, lbl=node.label, nm=node.name,
            )

    async def upsert_edge(self, team_id: int, edge: Edge) -> None:
        cypher = (
            "MERGE (a:Entity {team_id:$tid, label:$la, name:$na}) "
            "MERGE (b:Entity {team_id:$tid, label:$lb, name:$nb}) "
            f"MERGE (a)-[r:`{edge.rel}`]->(b)"
        )
        async with self.driver.session() as s:
            await s.run(
                cypher,
                tid=team_id,
                la=edge.src.label, na=edge.src.name,
                lb=edge.dst.label, nb=edge.dst.name,
            )

    async def neighbors(self, team_id, node, *, hops=1, limit=20):
        cypher = (
            f"MATCH (a:Entity {{team_id:$tid, label:$lbl, name:$nm}})-[r*1..{hops}]->(b:Entity) "
            "RETURN a, type(r[-1]) as rel, b LIMIT $lim"
        )
        out = []
        async with self.driver.session() as s:
            res = await s.run(cypher, tid=team_id, lbl=node.label, nm=node.name, lim=limit)
            async for rec in res:
                out.append(
                    (
                        Node(label=rec["a"]["label"], name=rec["a"]["name"]),
                        rec["rel"],
                        Node(label=rec["b"]["label"], name=rec["b"]["name"]),
                    )
                )
        return out

    async def find_nodes(self, team_id, names):
        lowers = [n.lower() for n in names]
        async with self.driver.session() as s:
            res = await s.run(
                "MATCH (n:Entity {team_id:$tid}) WHERE n.name IN $names RETURN n",
                tid=team_id, names=lowers,
            )
            return [Node(label=r["n"]["label"], name=r["n"]["name"]) async for r in res]
