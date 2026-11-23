from __future__ import annotations

from tana.domain.nodes import BaseNode
from tana.domain.search import SearchExpression
from tana.domain.types import NodeId
from tana.graph.graph import TanaGraph
from tana.query.search.evaluator import SearchEvaluator


def materialize_search(store: TanaGraph, search_node: BaseNode, expression: SearchExpression | None) -> list[NodeId]:
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
