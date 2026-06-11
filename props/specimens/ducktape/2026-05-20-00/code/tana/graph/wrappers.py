"""Helpers for structural wrapper nodes in Tana exports."""

from tana.domain.nodes import BaseNode

_WRAPPER_DOC_TYPES = frozenset({"workspace", "viewDef", "layout"})


def is_wrapper(node: BaseNode) -> bool:
    """Return True when the node represents a structural wrapper."""
    return node.props.doc_type in _WRAPPER_DOC_TYPES
