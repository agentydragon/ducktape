"""
Test to ensure that the HabitifyClient returns date objects, not strings.
"""

import datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest


@pytest.mark.asyncio
async def test_check_habit_status_returns_date_object(client):
    """Test that check_habit_status returns a date object for the date field."""
    # Create a mock response
    mock_resp = AsyncMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"data": {"status": "completed"}}

    # Patch the client's request method
    with patch.object(client.client, "get", return_value=mock_resp):
        # Call the method with a string date
        status = await client.check_habit_status("test-habit-id", "2025-01-15")

        # Verify the date is a Python date object, not a string
        assert isinstance(status.date, datetime.date)
        assert status.date.year == 2025
        assert status.date.month == 1
        assert status.date.day == 15

        # Call the method with a date object
        test_date = datetime.date(2025, 5, 20)
        status = await client.check_habit_status("test-habit-id", test_date)

        # Verify the date is the same Python date object we passed in
        assert isinstance(status.date, datetime.date)
        assert status.date == test_date


@pytest.mark.asyncio
async def test_set_habit_status_returns_date_object(client):
    """Test that set_habit_status returns a date object for the date field."""
    # Create a mock response
    mock_resp = AsyncMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"status": True}

    # Patch the client's request method
    with patch.object(client.client, "put", return_value=mock_resp):
        # Call the method with a string date
        status = await client.set_habit_status("test-habit-id", "completed", "2025-02-15", "Test note")

        # Verify the date is a Python date object, not a string
        assert isinstance(status.date, datetime.date)
        assert status.date.year == 2025
        assert status.date.month == 2
        assert status.date.day == 15

        # Call the method with a date object
        test_date = datetime.date(2025, 6, 10)
        status = await client.set_habit_status("test-habit-id", "completed", test_date, "Test note with date object")

        # Verify the date is the same Python date object we passed in
        assert isinstance(status.date, datetime.date)
        assert status.date == test_date
