from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from tana.domain.nodes import MIN_TUPLE_CHILDREN, BaseNode, TupleNode
from tana.domain.system_nodes import MEDIA_KEY_ID, MIME_TYPE_KEY_ID
from tana.domain.types import NodeId

if TYPE_CHECKING:
    from tana.graph.graph import TanaGraph


def get_tuple_value(node: BaseNode, key: NodeId | str, store: TanaGraph) -> BaseNode | None:
    """Return the first value node from a tuple keyed by `key`.

    Supports two shapes:
    - node is a tuple node: children[0] is the key id, children[1:] are values
    - node is a container: search its child tuple nodes for one where
      tuple.children[0] == key, then return the second child
    """
    # Case 1: node itself is a tuple — check its key
    if len(node.children) >= MIN_TUPLE_CHILDREN and node.children[0] == key:
        return store.get(node.children[1])

    # Case 2: search child tuples under this node
    for child in store.child_nodes(node):
        if len(child.children) >= MIN_TUPLE_CHILDREN and child.children[0] == key:
            return store.get(child.children[1])
    return None


def get_field_values(node: BaseNode, field_name: str, store: TanaGraph) -> Iterator[str]:
    """Get all values for a field as a list of strings."""
    for child in store.child_nodes(node):
        if (
            isinstance(child, TupleNode)
            and len(child.children) >= MIN_TUPLE_CHILDREN
            and (key_node := store.get(child.children[0]))
            and key_node.name == field_name
        ):
            for value_id in child.children[1:]:
                if (value_node := store.get(value_id)) and value_node.name:
                    yield value_node.name


def is_in_deleted_nodes(node: BaseNode, store: TanaGraph) -> bool:
    """Check if a node has 'Deleted Nodes' in its ancestor chain."""
    current: BaseNode | None = node
    visited = set()

    while current:
        if current.id in visited:
            break
        visited.add(current.id)

        if current.name and current.name == "Deleted Nodes":
            return True

        if current.props.owner_id:
            current = store.get(current.props.owner_id)
        else:
            break

    return False


def _metadata_value(node: BaseNode, key: NodeId, store: TanaGraph) -> str | None:
    """Name of the first value keyed by `key` in the node's metanode."""
    if node.props.meta_node_id and (metanode := store.get(node.props.meta_node_id)):
        val_node = get_tuple_value(metanode, key, store)
        return val_node.name if val_node else None
    return None


def get_image_url(node: BaseNode, store: TanaGraph) -> str | None:
    """Extract image URL from a visual node's metadata."""
    return _metadata_value(node, MEDIA_KEY_ID, store)


def get_mime_type(node: BaseNode, store: TanaGraph) -> str | None:
    """Extract MIME type from a node's metadata (e.g. "image/png")."""
    return _metadata_value(node, MIME_TYPE_KEY_ID, store)
