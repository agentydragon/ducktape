from __future__ import annotations

from tana.domain.constants import (
    AND_OPERATOR_ID,
    MIN_TUPLE_CHILDREN,
    NOT_OPERATOR_ID,
    OR_OPERATOR_ID,
    SEARCH_EXPRESSION_KEY_ID,
)
from tana.domain.nodes import BaseNode, TupleNode
from tana.domain.search import (
    BooleanOperator,
    BooleanSearch,
    FieldSearch,
    SearchExpression,
    TagSearch,
    TextSearch,
    TypeSearch,
)
from tana.domain.types import NodeId
from tana.graph.graph import TanaGraph


class SearchParseError(Exception):
    """Raised when a search expression cannot be parsed."""


def parse_search_expression(store: TanaGraph, search_node: BaseNode) -> SearchExpression | None:
    """
    Parse the search expression from a search node.

    Args:
        store: The graph containing all nodes
        search_node: The search node to parse

    Returns:
        The parsed search expression, or None if no expression found

    Raises:
        SearchParseError: If the expression cannot be parsed
    """
    if search_node.props.doc_type != "search":
        raise SearchParseError(f"Node {search_node.id} is not a search node")

    # Get metadata node
    if not search_node.props.meta_node_id:
        return None

    metadata = store.get(search_node.props.meta_node_id)
    if not metadata:
        raise SearchParseError(f"Metadata node {search_node.props.meta_node_id} not found")

    # Find search expression tuple in metadata children
    for child in store.child_nodes(metadata):
        if (
            isinstance(child, TupleNode)
            and len(child.children) >= MIN_TUPLE_CHILDREN
            and child.children[0] == SEARCH_EXPRESSION_KEY_ID
        ):
            return _parse_expression_components(store, child.children[1:])

    # Check if search expression is in view definition
    for child in store.child_nodes(metadata):
        if not isinstance(child, TupleNode):
            continue
        # Look for view definition tuples
        for grandchild_id in child.children:
            grandchild = store.get(grandchild_id)
            if grandchild and grandchild.props.doc_type == "viewDef":
                # Check view definition's children for search expression
                for ggchild in store.child_nodes(grandchild):
                    if (
                        isinstance(ggchild, TupleNode)
                        and len(ggchild.children) >= MIN_TUPLE_CHILDREN
                        and ggchild.children[0] == SEARCH_EXPRESSION_KEY_ID
                    ):
                        return _parse_expression_components(store, ggchild.children[1:])

    return None


def _parse_expression_components(store: TanaGraph, component_ids: list[NodeId]) -> SearchExpression | None:
    """
    Parse expression components into a SearchExpression.

    Args:
        store: The graph
        component_ids: List of node IDs representing the expression

    Returns:
        The parsed expression or None if empty
    """
    if not component_ids:
        return None

    expressions = [expr for comp_id in component_ids if (expr := _parse_single_component(store, comp_id))]

    if not expressions:
        return None
    if len(expressions) == 1:
        return expressions[0]
    return BooleanSearch(operator=BooleanOperator.AND, operands=expressions)


def _parse_tuple_operator(store: TanaGraph, node: TupleNode) -> SearchExpression | None:
    """Parse a tuple node that might be a boolean operator or field search."""
    if len(node.children) < MIN_TUPLE_CHILDREN:
        return None

    operator_map = {
        AND_OPERATOR_ID: BooleanOperator.AND,
        OR_OPERATOR_ID: BooleanOperator.OR,
        NOT_OPERATOR_ID: BooleanOperator.NOT,
    }

    operator_id = node.children[0]

    # Check if it's a boolean operator
    if operator_id in operator_map:
        return _parse_boolean_expression(store, operator_map[operator_id], node.children[1:])

    # Check if it's a field search
    operator_node = store.get(operator_id)
    if operator_node and operator_node.name:
        # Collect field values
        values = []
        for value_id in node.children[1:]:
            if (value_node := store.get(value_id)) and value_node.name:
                values.append(value_node.name)

        if values:
            return FieldSearch(field_name=operator_node.name, values=values)
        return TextSearch(text=f"<{operator_node.name}>")

    # Unknown operator
    raise SearchParseError(f"Unknown operator in search expression tuple: {operator_id}")


def _parse_single_component(store: TanaGraph, node_id: NodeId) -> SearchExpression | None:
    """
    Parse a single component node into a SearchExpression.

    Args:
        store: The graph
        node_id: The ID of the component node

    Returns:
        The parsed expression or None
    """
    node = store.get(node_id)
    if not node:
        return None

    # Check if it's a tuple (operator or field search)
    if isinstance(node, TupleNode):
        return _parse_tuple_operator(store, node)

    # Check if it's a system type
    if node.id.startswith("SYS_T"):
        return TypeSearch(type_id=node.id)

    # Check if it has a name - could be text search or tag
    if node.name:
        # If the node has supertags, it's likely a tag definition
        if store.get_supertags(node.id):
            return TagSearch(tag_id=node.id)
        # Otherwise treat as text search
        return TextSearch(text=node.name)

    # Default to tag search by ID
    return TagSearch(tag_id=node.id)


def _parse_boolean_expression(
    store: TanaGraph, operator: BooleanOperator, operand_ids: list[NodeId]
) -> BooleanSearch | None:
    """
    Parse a boolean expression with the given operator and operands.

    Args:
        store: The graph
        operator: The boolean operator
        operand_ids: List of operand node IDs

    Returns:
        The parsed boolean expression or None
    """
    operands = [expr for op_id in operand_ids if (expr := _parse_single_component(store, op_id))]
    return BooleanSearch(operator=operator, operands=operands) if operands else None
