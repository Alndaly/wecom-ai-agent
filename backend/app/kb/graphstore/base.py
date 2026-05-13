from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Node:
    label: str    # e.g. Product / Feature / Tag / User
    name: str     # canonical name (lower-cased)
    props: tuple = ()  # immutable

    def key(self) -> tuple[str, str]:
        return (self.label, self.name)


@dataclass
class Edge:
    src: Node
    dst: Node
    rel: str
    props: dict = field(default_factory=dict)


class GraphStore(ABC):
    name: str

    @abstractmethod
    async def upsert_node(self, team_id: int, node: Node) -> None: ...

    @abstractmethod
    async def upsert_edge(self, team_id: int, edge: Edge) -> None: ...

    @abstractmethod
    async def neighbors(
        self, team_id: int, node: Node, *, hops: int = 1, limit: int = 20
    ) -> list[tuple[Node, str, Node]]: ...

    @abstractmethod
    async def find_nodes(
        self, team_id: int, names: list[str]
    ) -> list[Node]: ...
