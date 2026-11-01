"""Ranges a History read page reports as deleted from stored history."""

from agentplane.history_service import history_service_pb2

# The generated protobuf stubs need the protobuf runtime as a direct mypy dependency.
# gazelle:include_dep @pypi//protobuf


def unstored_ranges(page: history_service_pb2.ReadSessionEventsResponse) -> tuple[tuple[int, int], ...]:
    """Every settlement and removal range reported by a read page, in sorted order."""
    ranges = [
        (cursor_range.first, cursor_range.last)
        for settlement in page.settlements
        for cursor_range in settlement.ranges
    ]
    ranges.extend((cursor_range.first, cursor_range.last) for cursor_range in page.removed)
    return tuple(sorted(ranges))
