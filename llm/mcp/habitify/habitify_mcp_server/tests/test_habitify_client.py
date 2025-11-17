"""
Tests for the Habitify API client.

Uses mock data based on the actual API responses seen in the reference YAML files.
"""

import datetime
from unittest.mock import MagicMock, patch

from hamcrest import all_of, assert_that, greater_than, has_length, has_properties, instance_of, only_contains
import httpx
import pytest

from ..habitify_client import HabitifyError
from ..types import Area, Habit, HabitStatus, Status


class TestHabitifyClient:
    """Tests for the Habitify client using async methods only."""

    @pytest.mark.asyncio
    async def test_get_habits(self, client, mock_async_response):
        """Test the get_habits method."""
        # Mock the response
        mock_resp = mock_async_response("get_habits.yaml")

        # Patch the client's request method
        with patch.object(client.client, "get", return_value=mock_resp) as mock_get:
            # Call the method
            habits = await client.get_habits()

            # Check that the correct URL was called
            mock_get.assert_called_once_with("/habits")

            # Check the returned data
            assert_that(habits, all_of(has_length(greater_than(0)), only_contains(instance_of(Habit))))

            # Check a specific habit attribute
            assert habits[0].id == "-Lo9NTLRX3aCxg-PjN25"
            assert not habits[0].archived

    @pytest.mark.asyncio
    async def test_get_habit(self, client, mock_async_response):
        """Test the get_habit method."""
        # Mock the response
        mock_resp = mock_async_response("get_habit_by_id.yaml")

        # Patch the client's request method
        with patch.object(client.client, "get", return_value=mock_resp) as mock_get:
            # Call the method
            habit = await client.get_habit("-Lo9NTLRX3aCxg-PjN25")

            # Check that the correct URL was called
            mock_get.assert_called_once_with("/habits/-Lo9NTLRX3aCxg-PjN25")

            # Check the returned data
            assert_that(habit, instance_of(Habit))
            assert habit.id == "-Lo9NTLRX3aCxg-PjN25"
            assert not habit.archived

    @pytest.mark.asyncio
    async def test_get_habit_not_found(self, client, mock_async_response):
        """Test the get_habit method with an invalid habit ID."""
        # Mock the error response
        mock_resp = mock_async_response("get_habit_invalid_id.yaml", status_code=500)
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "HTTP Error", request=MagicMock(), response=mock_resp
        )

        # Patch the client's request method
        with patch.object(client.client, "get", return_value=mock_resp) as mock_get:
            # Call the method and check for an exception
            with pytest.raises(HabitifyError) as excinfo:
                await client.get_habit("invalid-id-that-does-not-exist")

            # Check that the correct URL was called
            mock_get.assert_called_once_with("/habits/invalid-id-that-does-not-exist")

            # Check the error message
            assert "habit does not exist" in str(excinfo.value).lower()

    @pytest.mark.asyncio
    async def test_get_areas(self, client, mock_async_response):
        """Test the get_areas method."""
        # Mock the response
        mock_resp = mock_async_response("get_areas.yaml")

        # Patch the client's request method
        with patch.object(client.client, "get", return_value=mock_resp) as mock_get:
            # Call the method
            areas = await client.get_areas()

            # Check that the correct URL was called
            mock_get.assert_called_once_with("/areas")

            # Check the returned data
            assert_that(areas, all_of(has_length(greater_than(0)), only_contains(instance_of(Area))))

            # Check a specific area attribute
            assert areas[0].id == "-LrYlUBnzjyceYei_k5Z"
            assert areas[0].name == "H****h"

    @pytest.mark.asyncio
    async def test_get_journal(self, client, mock_async_response):
        """Test the get_journal method."""
        # Create a test date
        today = datetime.date.today().isoformat()

        # Mock the response
        mock_resp = mock_async_response("get_journal.yaml")

        # Patch the client's request method
        with patch.object(client.client, "get", return_value=mock_resp) as mock_get:
            # Call the method
            habits = await client.get_journal(date=today)

            # Check that the correct URL was called with parameters
            mock_get.assert_called_once()
            url = mock_get.call_args[0][0]
            params = mock_get.call_args[1]["params"]

            assert url == "/journal"
            assert "target_date" in params
            assert params["order_by"] == "priority"

            # Check the returned data
            assert_that(habits, only_contains(instance_of(Habit)))

    @pytest.mark.asyncio
    async def test_get_journal_filtered(self, client, mock_async_response):
        """Test the get_journal method with filters."""
        # Create a test date
        today = datetime.date.today().isoformat()

        # Mock the response
        mock_resp = mock_async_response("get_journal_filtered.yaml")

        # Patch the client's request method
        with patch.object(client.client, "get", return_value=mock_resp) as mock_get:
            # Call the method with filters
            habits = await client.get_journal(date=today, status="none", time_of_day="morning,evening")

            # Check that the correct URL was called with parameters
            mock_get.assert_called_once()
            url = mock_get.call_args[0][0]
            params = mock_get.call_args[1]["params"]

            assert url == "/journal"
            assert "target_date" in params
            assert params["status"] == "none"
            assert params["time_of_day"] == "morning,evening"

    @pytest.mark.asyncio
    async def test_check_habit_status(self, client, mock_async_response):
        """Test the check_habit_status method."""
        # Mock the response
        mock_resp = mock_async_response("get_habit_status.yaml")

        # Patch the client's request method
        with patch.object(client.client, "get", return_value=mock_resp) as mock_get:
            # Call the method
            status = await client.check_habit_status("-Lo9NTLRX3aCxg-PjN25", date="2025-05-09")

            # Check that the correct URL was called with parameters
            mock_get.assert_called_once()
            url = mock_get.call_args[0][0]
            params = mock_get.call_args[1]["params"]

            assert url == "/status/-Lo9NTLRX3aCxg-PjN25"
            assert "target_date" in params

            # Check the returned data
            assert_that(status, instance_of(HabitStatus))
            assert status.status == Status.COMPLETED

    @pytest.mark.asyncio
    async def test_check_habit_status_invalid_date(self, client, mock_async_response):
        """Test the check_habit_status method with an invalid date format."""
        # Mock the error response
        mock_resp = mock_async_response("get_habit_status_(invalid_date_format).yaml", status_code=500)
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "HTTP Error", request=MagicMock(), response=mock_resp
        )

        # Patch the client's request method
        with patch.object(client.client, "get", return_value=mock_resp) as mock_get:
            # Call the method and check for an exception
            with pytest.raises(HabitifyError) as excinfo:
                await client.check_habit_status("-Lo9NTLRX3aCxg-PjN25", date="2020-01-01")

            # Check that the correct URL was called
            mock_get.assert_called_once()

            # Check the error message
            assert "date format" in str(excinfo.value).lower()

    @pytest.mark.asyncio
    async def test_check_habit_status_range(self, client, mock_async_response):
        """Test the check_habit_status_range method."""
        # Mock the response for all date checks
        mock_resp = mock_async_response("get_habit_status.yaml")

        # Create a custom side_effect function to track which dates were requested
        requested_dates = []

        async def mock_get_with_date_tracking(url, **kwargs):
            # Record the requested date
            if "target_date" in kwargs.get("params", {}):
                target_date = kwargs["params"]["target_date"]
                requested_dates.append(target_date)
            return mock_resp

        # Patch the client's request method
        with patch.object(client.client, "get", side_effect=mock_get_with_date_tracking) as mock_get:
            # Call the method with a date range
            statuses = await client.check_habit_status_range(
                "-Lo9NTLRX3aCxg-PjN25", start_date="2025-05-01", end_date="2025-05-05"
            )

            # Check the total number of calls
            assert mock_get.call_count == 5

            # Check the returned data
            assert_that(statuses, all_of(has_length(5), only_contains(instance_of(HabitStatus))))

            # Check that dates are sorted in chronological order
            dates = [status.date for status in statuses]
            assert dates == sorted(dates)

    @pytest.mark.asyncio
    async def test_set_habit_status(self, client, mock_async_response):
        """Test the set_habit_status method."""
        # Mock the response
        mock_resp = mock_async_response("set_habit_status_(completed).yaml")

        # Patch the client's request method
        with patch.object(client.client, "put", return_value=mock_resp) as mock_put:
            # Call the method
            status = await client.set_habit_status(
                "-Lo9NTLRX3aCxg-PjN25",
                status=Status.COMPLETED,
                date="2025-05-09",
                note="Test completed via async unit test",
                value=1.0,
            )

            # Check that the correct URL was called with the right body
            mock_put.assert_called_once()
            url = mock_put.call_args[0][0]
            body = mock_put.call_args[1]["json"]

            assert url == "/status/-Lo9NTLRX3aCxg-PjN25"
            assert body["status"] == "completed"
            assert "target_date" in body
            assert body["note"] == "Test completed via async unit test"
            assert body["value"] == 1.0

            # Check the returned data
            assert_that(status, instance_of(HabitStatus))
            assert_that(
                status, has_properties(status=Status.COMPLETED, note="Test completed via async unit test", value=1.0)
            )

    @pytest.mark.asyncio
    async def test_set_habit_status_skipped(self, client, mock_async_response):
        """Test the set_habit_status method with skipped status."""
        # Mock the response
        mock_resp = mock_async_response("set_habit_status_(skipped).yaml")

        # Patch the client's request method
        with patch.object(client.client, "put", return_value=mock_resp) as mock_put:
            # Call the method
            status = await client.set_habit_status(
                "-Lo9NTLRX3aCxg-PjN25",
                status=Status.SKIPPED,
                date="2025-05-09",
                note="Test skipped via async unit test",
            )

            # Check that the correct URL was called with the right body
            mock_put.assert_called_once()
            url = mock_put.call_args[0][0]
            body = mock_put.call_args[1]["json"]

            assert url == "/status/-Lo9NTLRX3aCxg-PjN25"
            assert body["status"] == "skipped"
            assert "target_date" in body
            assert body["note"] == "Test skipped via async unit test"
            assert "value" not in body

            # Check the returned data
            assert_that(status, instance_of(HabitStatus))
            assert_that(
                status, has_properties(status=Status.SKIPPED, note="Test skipped via async unit test", value=None)
            )
