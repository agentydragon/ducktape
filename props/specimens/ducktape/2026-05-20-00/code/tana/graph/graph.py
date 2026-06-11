from __future__ import annotations

from collections import defaultdict
from collections.abc import ItemsView, Iterable, Iterator, Mapping, ValuesView
from typing import Any, cast, overload

from tana.domain.constants import LANGUAGE_KEY_ID, MIN_TUPLE_CHILDREN, SUPERTAG_KEY_ID
from tana.domain.nodes import DOC_CLASS, BaseNode, CodeBlockNode, TupleNode, UnknownNode
from tana.domain.types import NodeId
from tana.graph.wrappers import is_wrapper


class TanaGraph(Mapping[NodeId, BaseNode]):
    """In-memory graph representing a Tana workspace export."""

    def __init__(self, nodes: Mapping[NodeId, BaseNode]):
        self._nodes: dict[NodeId, BaseNode] = dict(nodes)
        self._supertag_index: dict[NodeId, list[str]] = {}
        self._build_supertag_index()

    # Mapping interface -----------------------------------------------------
    def __getitem__(self, node_id: NodeId) -> BaseNode:
        return self._nodes[node_id]

    def __iter__(self) -> Iterator[NodeId]:
        return iter(self._nodes)

    def __len__(self) -> int:
        return len(self._nodes)

    # Convenience helpers ---------------------------------------------------
    @overload
    def get(self, node_id: NodeId, /) -> BaseNode | None: ...

    @overload
    def get(self, node_id: NodeId, /, default: BaseNode) -> BaseNode: ...

    @overload
    def get[T](self, node_id: NodeId, /, default: T) -> BaseNode | T: ...

    def get[T](self, node_id: NodeId, /, default: BaseNode | T | None = None) -> BaseNode | T | None:
        return self._nodes.get(node_id, default)

    def values(self) -> ValuesView[BaseNode]:
        return self._nodes.values()

    def items(self) -> ItemsView[NodeId, BaseNode]:
        return self._nodes.items()

    def child_nodes(self, node: BaseNode) -> list[BaseNode]:
        """Return child nodes for a given node."""
        return [self._nodes[cid] for cid in node.children if cid in self._nodes]

    def get_supertags(self, node_id: NodeId) -> list[str]:
        return self._supertag_index.get(node_id, [])

    def has_supertag(self, node_id: NodeId, tag: str) -> bool:
        return tag in self._supertag_index.get(node_id, [])

    def get_language(self, node: CodeBlockNode) -> str:
        """Extract programming language from a code block node."""
        for child in self.child_nodes(node):
            if isinstance(child, TupleNode) and len(child.children) >= 2:
                key_id = child.children[0]
                if key_id == LANGUAGE_KEY_ID:
                    val_nodes = self.child_nodes(child)
                    if len(val_nodes) >= 2 and isinstance(val_nodes[1], BaseNode) and val_nodes[1].name:
                        return val_nodes[1].name
                    return ""
        return ""

    # Construction ----------------------------------------------------------
    @classmethod
    def from_documents(cls, documents: Iterable[dict[str, Any]]) -> TanaGraph:
        """Build a graph from raw JSON documents."""

        def _make_node(raw: dict[str, Any]) -> BaseNode:
            doc_type = raw.get("props", {}).get("_docType")
            node_model = DOC_CLASS.get(doc_type, UnknownNode)
            # All DOC_CLASS values are BaseNode subclasses; model_validate returns the specific type
            return cast(BaseNode, node_model.model_validate(raw))

        mapping = {NodeId(str(doc["id"])): _make_node(doc) for doc in documents}
        return cls(mapping)

    # Internal --------------------------------------------------------------
    def _build_supertag_index(self) -> None:
        idx: defaultdict[NodeId, list[str]] = defaultdict(list)

        def _add(node_id: NodeId, tags: list[str]) -> None:
            for tag in tags:
                if tag and tag not in idx[node_id]:
                    idx[node_id].append(tag)

        for node in self._nodes.values():
            if not (
                isinstance(node, TupleNode)
                and len(node.children) >= MIN_TUPLE_CHILDREN
                and node.props.owner_id
                and (key_node := self.get(node.children[0]))
                and key_node.id == SUPERTAG_KEY_ID
            ):
                continue
            for value_node in self.child_nodes(node)[1:]:
                if value_node.name:
                    idx[node.props.owner_id].append(value_node.name)

        for node in self._nodes.values():
            if node.props.meta_node_id:
                _add(node.id, list(idx[node.props.meta_node_id]))

        for wrapper in self._nodes.values():
            if is_wrapper(wrapper):
                for child_id in wrapper.children:
                    _add(child_id, list(idx[wrapper.id]))

        self._supertag_index = dict(idx)
