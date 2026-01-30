from __future__ import annotations

from collections.abc import Iterator
from functools import singledispatch

from tana.domain.constants import EVENT_TYPE_ID, MEETING_TYPE_ID
from tana.domain.nodes import BaseNode
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
from tana.query.filters import filter_by_field_value, filter_by_tag, filter_nodes


class SearchEvaluator:
    """Evaluates search expressions against a TanaGraph."""

    def __init__(
        self, store: TanaGraph, skip_trash: bool = True, skip_deleted: bool = True, parent_node: BaseNode | None = None
    ):
        """
        Initialize the search evaluator.

        Args:
            store: The graph to search
            skip_trash: Skip nodes in trash (default True)
            skip_deleted: Skip nodes under "Deleted Nodes" (default True)
            parent_node: Parent node for resolving PARENT references
        """
        self.store = store
        self.skip_trash = skip_trash
        self.skip_deleted = skip_deleted
        self.parent_node = parent_node

    def evaluate(self, expression: SearchExpression, context: BaseNode | None = None) -> Iterator[BaseNode]:
        """
        Evaluate a search expression.

        Args:
            expression: The search expression to evaluate
            context: Optional context node to limit search scope

        Yields:
            Nodes matching the search expression
        """
        # Get base results
        results = _evaluate_dispatch(expression, self)

        # Apply context filter if provided
        if context:
            context_descendants = set(self._get_descendants(context))
            results = (node for node in results if node.id in context_descendants)

        yield from results

    def _evaluate_tag(self, tag_node_id: NodeId) -> Iterator[BaseNode]:
        """
        Find all nodes with a specific tag.

        Args:
            tag_node_id: The ID of the tag node

        Yields:
            Nodes with the specified tag
        """
        # First check if the tag node exists and get its name
        tag_node = self.store.get(tag_node_id)
        if not tag_node or not tag_node.name:
            return

        # Use the existing filter_by_tag function
        yield from filter_by_tag(self.store, tag_node.name, self.skip_trash, self.skip_deleted)

    def _evaluate_type(self, type_node_id: NodeId) -> Iterator[BaseNode]:
        """
        Find all nodes of a specific system type.

        Args:
            type_node_id: The ID of the system type

        Yields:
            Nodes of the specified type
        """
        # Map system type IDs to doc_type values
        type_map = {EVENT_TYPE_ID: "event", MEETING_TYPE_ID: "meeting"}

        doc_type = type_map.get(type_node_id)
        if not doc_type:
            raise ValueError(f"Unknown system type: {type_node_id}")

        def matches_type(node: BaseNode) -> bool:
            return node.props.doc_type == doc_type

        yield from filter_nodes(self.store, matches_type, self.skip_trash, self.skip_deleted)

    def _evaluate_text(self, text: str) -> Iterator[BaseNode]:
        """
        Find nodes matching text criteria.

        Args:
            text: The text to search for

        Yields:
            Nodes matching the text
        """
        # Special cases for unsupported operators
        if text in ("FROM CALENDAR", "<DATE OVERLAPS>", "<Event status>"):
            # For now, return empty results for unsupported operators
            return

        # General text search - match nodes by name
        text_lower = text.lower()

        def matches_text(node: BaseNode) -> bool:
            return bool(node.name and text_lower in node.name.lower())

        yield from filter_nodes(self.store, matches_text, self.skip_trash, self.skip_deleted)

    def _evaluate_field(self, field_name: str, values: list[str]) -> Iterator[BaseNode]:
        """
        Find nodes with specific field values.

        Args:
            field_name: The field to search
            values: The values to match

        Yields:
            Nodes matching the field criteria
        """
        # Resolve PARENT references
        resolved_values = []
        for value in values:
            if value == "PARENT" and self.parent_node and self.parent_node.name:
                resolved_values.append(self.parent_node.name)
            else:
                resolved_values.append(value)

        yield from filter_by_field_value(
            self.store,
            field_name,
            allowed_values=set(resolved_values),
            skip_trash=self.skip_trash,
            skip_deleted=self.skip_deleted,
        )

    def _evaluate_boolean(self, operator: BooleanOperator, operands: list[SearchExpression]) -> Iterator[BaseNode]:
        """
        Evaluate a boolean expression.

        Args:
            operator: The boolean operator
            operands: List of operand expressions

        Yields:
            Nodes matching the boolean expression
        """
        if not operands:
            return

        if operator == BooleanOperator.OR:
            # Union of all operand results
            seen: set[NodeId] = set()
            for operand in operands:
                for node in _evaluate_dispatch(operand, self):
                    if node.id not in seen:
                        seen.add(node.id)
                        yield node

        elif operator == BooleanOperator.AND:
            # Intersection of all operand results
            # Evaluate first operand
            result_sets = [{node.id for node in _evaluate_dispatch(operands[0], self)}]

            # Evaluate remaining operands and intersect
            for operand in operands[1:]:
                operand_ids = {node.id for node in _evaluate_dispatch(operand, self)}
                result_sets[0] &= operand_ids

            # Yield nodes in the intersection
            for node_id in result_sets[0]:
                result_node: BaseNode | None = self.store.get(node_id)
                if result_node is not None:
                    yield result_node

        elif operator == BooleanOperator.NOT:
            # All nodes except those matching the operand
            if len(operands) != 1:
                raise ValueError(f"NOT operator requires exactly 1 operand, got {len(operands)}")

            excluded_ids = {node.id for node in _evaluate_dispatch(operands[0], self)}

            def not_excluded(node: BaseNode) -> bool:
                return node.id not in excluded_ids

            yield from filter_nodes(self.store, not_excluded, self.skip_trash, self.skip_deleted)

    def _get_descendants(self, node: BaseNode) -> set[NodeId]:
        """
        Get all descendants of a node (including the node itself).

        Args:
            node: The root node

        Returns:
            Set of descendant node IDs
        """
        descendants = {node.id}
        to_process = [node]

        while to_process:
            current = to_process.pop()
            for child in self.store.child_nodes(current):
                if child.id not in descendants:
                    descendants.add(child.id)
                    to_process.append(child)

        return descendants


@singledispatch
def _evaluate_dispatch(expression: SearchExpression, evaluator: SearchEvaluator) -> Iterator[BaseNode]:
    """Dispatch search expression evaluation based on type."""
    raise ValueError(f"Unknown expression type: {type(expression)}")


@_evaluate_dispatch.register(TagSearch)
def _(expression: TagSearch, evaluator: SearchEvaluator) -> Iterator[BaseNode]:
    yield from evaluator._evaluate_tag(expression.tag_id)


@_evaluate_dispatch.register(TypeSearch)
def _(expression: TypeSearch, evaluator: SearchEvaluator) -> Iterator[BaseNode]:
    yield from evaluator._evaluate_type(expression.type_id)


@_evaluate_dispatch.register(TextSearch)
def _(expression: TextSearch, evaluator: SearchEvaluator) -> Iterator[BaseNode]:
    yield from evaluator._evaluate_text(expression.text)


@_evaluate_dispatch.register(FieldSearch)
def _(expression: FieldSearch, evaluator: SearchEvaluator) -> Iterator[BaseNode]:
    yield from evaluator._evaluate_field(expression.field_name, expression.values)


@_evaluate_dispatch.register(BooleanSearch)
def _(expression: BooleanSearch, evaluator: SearchEvaluator) -> Iterator[BaseNode]:
    yield from evaluator._evaluate_boolean(expression.operator, expression.operands)
