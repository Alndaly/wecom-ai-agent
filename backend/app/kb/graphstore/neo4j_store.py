"""Neo4j adapter — opt-in (requires `neo4j` driver).

Schema (multi-tenant via property):
  (:Entity {team_id, label, name})
  Edges typed by `rel` value (sanitised to A-Za-z0-9_ for Cypher safety).

Edge cases handled:
  - Relationship type must be a valid identifier; we sanitise.
  - All writes use MERGE for idempotency.
  - 1-hop neighbours follows outgoing edges only (matches Memory impl).
"""
from __future__ import annotations

import re

from .base import Edge, GraphStore, Node

_REL_SAFE = re.compile(r"[^A-Za-z0-9_]+")


def _safe_rel(rel: str) -> str:
    cleaned = _REL_SAFE.sub("_", rel or "RELATED")
    return cleaned or "RELATED"


class Neo4jGraphStore(GraphStore):
    name = "neo4j"

    def __init__(self, *, uri: str, user: str, password: str) -> None:
        from neo4j import AsyncGraphDatabase  # type: ignore

        self.driver = AsyncGraphDatabase.driver(uri, auth=(user, password))

    async def close(self) -> None:
        await self.driver.close()

    async def upsert_node(self, team_id: int, node: Node) -> None:
        async with self.driver.session() as s:
            await s.run(
                "MERGE (n:Entity {team_id:$tid, label:$lbl, name:$nm})",
                tid=team_id, lbl=node.label, nm=node.name,
            )

    async def upsert_edge(self, team_id: int, edge: Edge) -> None:
        rel = _safe_rel(edge.rel)
        cypher = (
            "MERGE (a:Entity {team_id:$tid, label:$la, name:$na}) "
            "MERGE (b:Entity {team_id:$tid, label:$lb, name:$nb}) "
            f"MERGE (a)-[r:`{rel}`]->(b)"
        )
        async with self.driver.session() as s:
            await s.run(
                cypher,
                tid=team_id,
                la=edge.src.label, na=edge.src.name,
                lb=edge.dst.label, nb=edge.dst.name,
            )

    async def neighbors(self, team_id, node, *, hops=1, limit=20):
        hops = max(1, min(hops, 3))
        cypher = (
            f"MATCH (a:Entity {{team_id:$tid, label:$lbl, name:$nm}})-[r*1..{hops}]->(b:Entity) "
            "WHERE b.team_id = $tid "
            "RETURN a, type(r[-1]) as rel, b LIMIT $lim"
        )
        out = []
        async with self.driver.session() as s:
            res = await s.run(cypher, tid=team_id, lbl=node.label, nm=node.name, lim=limit)
            async for rec in res:
                a = rec["a"]
                b = rec["b"]
                out.append(
                    (
                        Node(label=a["label"], name=a["name"]),
                        rec["rel"],
                        Node(label=b["label"], name=b["name"]),
                    )
                )
        return out

    async def delete_chunks(self, team_id: int, chunk_ids: list[int]) -> None:
        if not chunk_ids:
            return
        names = [f"chunk-{cid}" for cid in chunk_ids]
        async with self.driver.session() as s:
            await s.run(
                "MATCH (c:Entity {team_id:$tid, label:'Chunk'}) "
                "WHERE c.name IN $names DETACH DELETE c",
                tid=team_id, names=names,
            )

    async def find_nodes(self, team_id, names):
        lowers = [n.lower() for n in names]
        async with self.driver.session() as s:
            res = await s.run(
                "MATCH (n:Entity {team_id:$tid}) WHERE n.name IN $names RETURN n",
                tid=team_id, names=lowers,
            )
            return [Node(label=r["n"]["label"], name=r["n"]["name"]) async for r in res]

    async def related_chunks(
        self, team_id: int, seed_chunk_ids: list[int], *, limit: int = 8
    ) -> list[int]:
        seed_names = [f"chunk-{cid}" for cid in seed_chunk_ids]
        if not seed_names:
            return []
        async with self.driver.session() as s:
            res = await s.run(
                """
                MATCH (seed:Entity {team_id:$tid, label:'Chunk'})
                WHERE seed.name IN $seed_names
                MATCH (seed)-[:MENTIONS]->(entity:Entity)
                MATCH (related:Entity {team_id:$tid, label:'Chunk'})-[:MENTIONS]->(entity)
                WHERE NOT related.name IN $seed_names
                WITH related, count(DISTINCT entity) AS overlap
                ORDER BY overlap DESC, related.name ASC
                RETURN related.name AS name
                LIMIT $lim
                """,
                tid=team_id,
                seed_names=seed_names,
                lim=limit,
            )
            out: list[int] = []
            async for rec in res:
                name = str(rec["name"] or "")
                try:
                    out.append(int(name.removeprefix("chunk-")))
                except ValueError:
                    continue
            return out
