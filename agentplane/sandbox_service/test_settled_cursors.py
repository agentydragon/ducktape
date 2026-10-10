import pytest
import pytest_bazel

from agentplane.sandbox_service.settled_cursors import next_stored, previous_stored

# Adjacent ranges from two settlements, and a lone settled cursor.
SETTLED = ((3, 5), (6, 8), (11, 11))


@pytest.mark.parametrize(("cursor", "expected"), [(0, 1), (2, 9), (5, 9), (9, 10), (10, 12)])
def test_next_stored_skips_settled_ranges(cursor: int, expected: int) -> None:
    assert next_stored(cursor, SETTLED) == expected


@pytest.mark.parametrize(("cursor", "expected"), [(13, 12), (12, 10), (9, 2), (2, 1)])
def test_previous_stored_skips_settled_ranges(cursor: int, expected: int) -> None:
    assert previous_stored(cursor, SETTLED) == expected


if __name__ == "__main__":
    pytest_bazel.main()
