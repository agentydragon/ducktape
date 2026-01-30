"""Core query helpers for Tana export logic."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tana.domain.types import NodeId

if TYPE_CHECKING:
    from tana.domain.nodes import BaseNode
    from tana.graph.workspace import TanaGraph


def get_tuple_value(node: BaseNode, key: NodeId | str, store: TanaGraph) -> BaseNode | None:
    """Return the first value node from a tuple keyed by `key`.

    Supports two shapes:
    - node is a tuple node: children[0] is the key id, children[1:] are values
    - node is a container: search its child tuple nodes for one where
      tuple.children[0] == key, then return tuple.child_nodes[1]
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
