from __future__ import annotations

from collections.abc import Callable, Iterator

from tana.domain.nodes import BaseNode
from tana.domain.types import NodeId
from tana.graph.graph import TanaGraph
from tana.query.nodes import get_field_values, is_in_deleted_nodes


def filter_nodes(
    store: TanaGraph, predicate: Callable[[BaseNode], bool], *, skip_trash: bool = True, skip_deleted: bool = True
) -> Iterator[BaseNode]:
    """
    Filter nodes in a TanaGraph based on a predicate function.

    Args:
        store: The graph to filter
        predicate: Function that returns True for nodes to include
        skip_trash: Skip nodes in trash (default True)
        skip_deleted: Skip nodes under "Deleted Nodes" (default True)

    Yields:
        Nodes that match the predicate
    """
    for node in store.values():
        # Skip trash nodes if requested
        if skip_trash and node.is_trash:
            continue

        # Skip deleted nodes if requested
        if skip_deleted and is_in_deleted_nodes(node, store):
            continue

        # Apply the predicate
        if predicate(node):
            yield node


def filter_by_tag(
    store: TanaGraph, tag_name: str, *, skip_trash: bool = True, skip_deleted: bool = True
) -> Iterator[BaseNode]:
    """
    Filter nodes by supertag.

    Requires the graph to provide supertag indexes (built by TanaGraph).

    Args:
        store: The graph to filter
        tag_name: The tag to filter by
        skip_trash: Skip nodes in trash (default True)
        skip_deleted: Skip nodes under "Deleted Nodes" (default True)

    Yields:
        Nodes that have the specified tag
    """

    def has_tag(node: BaseNode) -> bool:
        return store.has_supertag(node.id, tag_name)

    return filter_nodes(store, has_tag, skip_trash=skip_trash, skip_deleted=skip_deleted)


def filter_by_field_value(
    store: TanaGraph,
    field_name: str,
    allowed_values: set[str] | None = None,
    excluded_values: set[str] | None = None,
    *,
    skip_trash: bool = True,
    skip_deleted: bool = True,
) -> Iterator[BaseNode]:
    """
    Filter nodes by field values.

    Args:
        store: The graph to filter
        field_name: The field name to check
        allowed_values: If provided, only include nodes with these values
        excluded_values: If provided, exclude nodes with these values
        skip_trash: Skip nodes in trash (default True)
        skip_deleted: Skip nodes under "Deleted Nodes" (default True)

    Yields:
        Nodes that match the field value criteria
    """

    def matches_criteria(node: BaseNode) -> bool:
        values = set(get_field_values(node, field_name, store))

        if not values:
            return False

        if allowed_values and not (values & allowed_values):
            return False

        return not (excluded_values and values & excluded_values)

    return filter_nodes(store, matches_criteria, skip_trash=skip_trash, skip_deleted=skip_deleted)


def filter_open_issues(store: TanaGraph) -> Iterator[NodeId]:
    """
    Find all nodes with #issue tag where Status is not Done/Cancelled/Shelved.

    This is a specialized filter for issue tracking.

    Args:
        store: The graph to search

    Yields:
        Node IDs of open issues
    """
    # First filter by issue tag
    issue_nodes = filter_by_tag(store, "issue")

    # Then filter by status
    for node in issue_nodes:
        status_values = list(get_field_values(node, "Status", store))

        # Skip if no status field
        if not status_values:
            continue

        # Check if Status is Done, Cancelled, or Shelved
        if {status.lower() for status in status_values} & {"done", "cancelled", "shelved"}:
            continue

        yield node.id
