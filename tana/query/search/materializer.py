from __future__ import annotations

from tana.domain.nodes import BaseNode
from tana.domain.types import NodeId
from tana.graph.graph import TanaGraph
from tana.query.search.evaluator import SearchEvaluator
from tana.query.search.parser import parse_search_expression


def materialize_search(store: TanaGraph, search_node: BaseNode) -> list[NodeId]:
    expression = parse_search_expression(store, search_node)
    if not expression:
        return []

    context = None
    if search_node.props.search_context_node:
        context = store.get(NodeId(search_node.props.search_context_node))

    # Get parent node for PARENT resolution
    parent_node = None
    if search_node.props.owner_id:
        parent_node = store.get(search_node.props.owner_id)

    evaluator = SearchEvaluator(store, parent_node=parent_node)
    results = evaluator.evaluate(expression, context)

    return [node.id for node in results]


def compare_search_results(store: TanaGraph, search_node: BaseNode) -> dict[str, list[NodeId]]:
    stored_results = search_node.children

    materialized_results = materialize_search(store, search_node)

    stored_set = set(stored_results)
    materialized_set = set(materialized_results)

    return {
        "stored": stored_results,
        "materialized": materialized_results,
        "missing": list(stored_set - materialized_set),
        "extra": list(materialized_set - stored_set),
    }
