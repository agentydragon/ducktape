"""
Test that the HabitifyClient returns HabitStatus models with ISO date strings (YYYY-MM-DD).
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from habitify.types import HabitStatus

# Uses 'client' fixture from conftest.py


@pytest.mark.asyncio
async def test_check_habit_status_returns_iso_date_string(client):
    """Test that check_habit_status returns HabitStatus with an ISO date string."""
    # Create a mock response
    mock_resp = AsyncMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"data": {"status": "completed"}}

    # Patch the client's request method
    with patch.object(client.client, "get", return_value=mock_resp):
        # Call the method with a string date
        status = await client.check_habit_status("test-habit-id", "2025-01-15")

        # Verify we get a HabitStatus
        assert isinstance(status, HabitStatus)

        # Verify the date is an ISO string (YYYY-MM-DD), not a date object
        assert isinstance(status.date, str)
        assert status.date == "2025-01-15"


@pytest.mark.asyncio
async def test_set_habit_status_returns_iso_date_string(client):
    """Test that set_habit_status returns HabitStatus with an ISO date string."""
    # Create a mock response
    mock_resp = AsyncMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"status": True}

    # Patch the client's request method
    with patch.object(client.client, "put", return_value=mock_resp):
        # Call the method with a string date
        status = await client.set_habit_status("test-habit-id", "completed", "2025-02-15", "Test note")

        # Verify we get a HabitStatus
        assert isinstance(status, HabitStatus)

        # Verify the date is an ISO string (YYYY-MM-DD), not a date object
        assert isinstance(status.date, str)
        assert status.date == "2025-02-15"


@pytest.mark.asyncio
async def test_check_habit_status_range_returns_iso_date_strings(client):
    """Test that check_habit_status_range returns list of HabitStatus with ISO date strings."""
    # Create a mock response for all date checks
    mock_resp = AsyncMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"data": {"status": "completed"}}

    # Patch the client's request method to return our mock response
    with patch.object(client.client, "get", return_value=mock_resp) as mock_get:
        # Call the method with a date range (3 days)
        statuses = await client.check_habit_status_range("test-habit-id", start_date="2025-03-15", days=3)

        # Should have made 3 calls for 3 days
        assert mock_get.call_count == 3

        # Should return 3 status objects
        assert len(statuses) == 3

        # All should be HabitStatus with ISO date strings
        expected_dates = {"2025-03-15", "2025-03-16", "2025-03-17"}
        actual_dates = {status.date for status in statuses}

        for status in statuses:
            assert isinstance(status, HabitStatus)
            assert isinstance(status.date, str)
            assert status.date in expected_dates

        assert actual_dates == expected_dates
