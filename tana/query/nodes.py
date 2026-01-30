from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from tana.domain.constants import MEDIA_KEY_ID, MIN_TUPLE_CHILDREN
from tana.domain.nodes import BaseNode, TupleNode
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
    key_str = str(key)

    children = list(node.children)

    # Case 1: node itself is a tuple — check its key
    if children:
        first = children[0]
        if str(first) == key_str and len(children) >= 2:
            return store.get(children[1])

    # Case 2: search child tuples under this node
    for cid in children:
        try:
            t = store[cid]
        except KeyError:
            continue
        t_children = list(t.children)
        if not t_children:
            continue
        if str(t_children[0]) != key_str:
            continue
        if len(t_children) >= 2:
            return store.get(t_children[1])
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


def get_ancestors(node: BaseNode, store: TanaGraph) -> list[BaseNode]:
    """Get all ancestors of a node, from immediate parent to root."""
    ancestors = []
    current = node
    visited = set()

    while current.props.owner_id and current.props.owner_id not in visited:
        visited.add(current.id)
        if parent := store.get(current.props.owner_id):
            ancestors.append(parent)
            current = parent
        else:
            break

    return ancestors


def find_nodes_by_tag(store: TanaGraph, tag_name: str) -> Iterator[BaseNode]:
    """Find all nodes with a specific supertag."""
    for node in store.values():
        if store.has_supertag(node.id, tag_name):
            yield node


def get_image_url(node: BaseNode, store: TanaGraph) -> str | None:
    """Extract image URL from a visual node's metadata."""
    if not node.props.meta_node_id:
        return None

    metanode = store.get(node.props.meta_node_id)
    if not metanode:
        return None

    val_node = get_tuple_value(metanode, MEDIA_KEY_ID, store)
    if isinstance(val_node, BaseNode):
        return val_node.name
    return None
