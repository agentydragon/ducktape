"""Walking a Session history whose settled delta cursors are no longer stored.

Settlement removes whole cursor ranges from a Session's history. A read page reports the ranges
that intersect it, and a reader treats a cursor inside one as covered, not missing.
"""

from collections.abc import Iterable

from agentplane.proto import history_service_pb2

# The generated protobuf stubs need the protobuf runtime as a direct mypy dependency.
# gazelle:include_dep @pypi//protobuf


def settled_ranges(ranges: Iterable[history_service_pb2.CursorRange]) -> tuple[tuple[int, int], ...]:
    return tuple(sorted((cursor_range.first, cursor_range.last) for cursor_range in ranges))


def next_stored(cursor: int, settled: Iterable[tuple[int, int]]) -> int:
    """The first cursor after `cursor` that is not inside a settled range."""
    following = cursor + 1
    for first, last in sorted(settled):
        if first <= following <= last:
            following = last + 1
    return following
