from __future__ import annotations

from pathlib import Path

from tana.domain.nodes import BaseNode
from tana.domain.types import NodeId
from tana.graph.graph import TanaGraph
from tana.graph.loader import load_workspace
from tana.query.search.materializer import compare_search_results, materialize_search


class Workspace:
    """High-level facade around a TanaGraph."""

    def __init__(self, graph: TanaGraph) -> None:
        self.graph = graph

    @classmethod
    def load(cls, path: Path | str) -> Workspace:
        graph = load_workspace(Path(path))
        return cls(graph)

    def node(self, node_id: NodeId) -> BaseNode:
        return self.graph[node_id]

    def materialize_search(self, node_id: NodeId) -> list[NodeId]:
        return materialize_search(self.graph, self.node(node_id))

    def compare_search_results(self, node_id: NodeId) -> dict[str, list[NodeId]]:
        return compare_search_results(self.graph, self.node(node_id))
