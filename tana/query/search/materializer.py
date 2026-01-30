from __future__ import annotations

from tana.domain.nodes import BaseNode
from tana.domain.types import NodeId
from tana.graph.graph import TanaGraph
from tana.query.search.evaluator import SearchEvaluator
from tana.query.search.parser import parse_search_expression


def materialize_search(store: TanaGraph, search_node: BaseNode) -> list[NodeId]:
    """
    Materialize a search node by executing its search expression.

    Args:
        store: The graph containing all nodes
        search_node: The search node to materialize

    Returns:
        List of node IDs matching the search
    """
    # Parse the search expression
    expression = parse_search_expression(store, search_node)
    if not expression:
        return []

    # Get search context if specified
    context = None
    if search_node.props.search_context_node:
        context = store.get(NodeId(search_node.props.search_context_node))

    # Get parent node for PARENT resolution
    parent_node = None
    if search_node.props.owner_id:
        parent_node = store.get(search_node.props.owner_id)

    # Create evaluator and execute search
    evaluator = SearchEvaluator(store, parent_node=parent_node)
    results = evaluator.evaluate(expression, context)

    # Collect and return node IDs
    return [node.id for node in results]


def compare_search_results(store: TanaGraph, search_node: BaseNode) -> dict[str, list[NodeId]]:
    """
    Compare stored search results with re-executed results.

    Args:
        store: The graph
        search_node: The search node to compare

    Returns:
        Dictionary with 'stored', 'materialized', 'missing', and 'extra' results
    """
    # Get stored results
    stored_results = search_node.children

    # Get materialized results
    materialized_results = materialize_search(store, search_node)

    # Convert to sets for comparison
    stored_set = set(stored_results)
    materialized_set = set(materialized_results)

    return {
        "stored": stored_results,
        "materialized": materialized_results,
        "missing": list(stored_set - materialized_set),
        "extra": list(materialized_set - stored_set),
    }
