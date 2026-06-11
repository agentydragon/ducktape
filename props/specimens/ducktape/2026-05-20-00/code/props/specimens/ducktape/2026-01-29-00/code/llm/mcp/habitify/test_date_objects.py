"""
Test to ensure that the HabitifyClient returns datetime objects, not strings.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest_bazel

from habitify.types import Status


async def test_check_habit_status_returns_datetime_object(client):
    """Test that check_habit_status returns a datetime object for the date field."""
    # Create a mock response
    mock_resp = AsyncMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"data": {"status": "completed"}}

    # Patch the client's request method
    with patch.object(client.client, "get", return_value=mock_resp):
        # Call the method with a string date
        status = await client.check_habit_status("test-habit-id", "2025-01-15")

        # Verify the date is a timezone-aware datetime object
        assert isinstance(status.date, datetime)
        assert status.date.tzinfo is not None
        assert status.date.year == 2025
        assert status.date.month == 1
        assert status.date.day == 15

        # Call the method with a datetime object
        test_datetime = datetime(2025, 5, 20, 12, 30, tzinfo=UTC)
        status = await client.check_habit_status("test-habit-id", test_datetime)

        # Verify the date is a datetime object
        assert isinstance(status.date, datetime)
        assert status.date.tzinfo is not None
        assert status.date.year == 2025
        assert status.date.month == 5
        assert status.date.day == 20


async def test_set_habit_status_returns_datetime_object(client):
    """Test that set_habit_status returns a datetime object for the date field."""
    # Create a mock response
    mock_resp = AsyncMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"status": True}

    # Patch the client's request method
    with patch.object(client.client, "put", return_value=mock_resp):
        # Call the method with a string date
        status = await client.set_habit_status("test-habit-id", Status.COMPLETED, "2025-02-15", "Test note")

        # Verify the date is a timezone-aware datetime object
        assert isinstance(status.date, datetime)
        assert status.date.tzinfo is not None
        assert status.date.year == 2025
        assert status.date.month == 2
        assert status.date.day == 15

        # Call the method with a datetime object
        test_datetime = datetime(2025, 6, 10, 14, 0, tzinfo=UTC)
        status = await client.set_habit_status(
            "test-habit-id", Status.COMPLETED, test_datetime, "Test note with datetime object"
        )

        # Verify the date is a datetime object
        assert isinstance(status.date, datetime)
        assert status.date.tzinfo is not None
        assert status.date.year == 2025
        assert status.date.month == 6
        assert status.date.day == 10


if __name__ == "__main__":
    pytest_bazel.main()
