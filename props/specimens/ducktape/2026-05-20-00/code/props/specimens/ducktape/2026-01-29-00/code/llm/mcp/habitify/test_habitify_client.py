"""Tests for the Habitify API client.

Uses mock data based on the actual API responses seen in the reference YAML files.
All tests use async methods only.
"""

import datetime
from unittest.mock import MagicMock

import httpx
import pytest
import pytest_bazel
from hamcrest import all_of, assert_that, greater_than, has_length, has_properties, instance_of, only_contains

from habitify.habitify_client import HabitifyError
from habitify.types import Area, Habit, HabitStatus, Status


async def test_get_habits(client, mock_async_response, patch_client_method):
    mock_resp = mock_async_response("get_habits.yaml")

    with patch_client_method("get", return_value=mock_resp) as mock_get:
        habits = await client.get_habits()

        mock_get.assert_called_once_with("/habits")
        assert_that(habits, all_of(has_length(greater_than(0)), only_contains(instance_of(Habit))))
        assert habits[0].id == "-Lo9NTLRX3aCxg-PjN25"
        assert not habits[0].archived


async def test_get_habit(client, mock_async_response, patch_client_method):
    mock_resp = mock_async_response("get_habit_by_id.yaml")

    with patch_client_method("get", return_value=mock_resp) as mock_get:
        habit = await client.get_habit("-Lo9NTLRX3aCxg-PjN25")

        mock_get.assert_called_once_with("/habits/-Lo9NTLRX3aCxg-PjN25")
        assert_that(habit, instance_of(Habit))
        assert habit.id == "-Lo9NTLRX3aCxg-PjN25"
        assert not habit.archived


async def test_get_habit_not_found(client, mock_async_response, patch_client_method):
    mock_resp = mock_async_response("get_habit_invalid_id.yaml", status_code=500)
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "HTTP Error", request=MagicMock(), response=mock_resp
    )

    with patch_client_method("get", return_value=mock_resp) as mock_get:
        with pytest.raises(HabitifyError) as excinfo:
            await client.get_habit("invalid-id-that-does-not-exist")

        mock_get.assert_called_once_with("/habits/invalid-id-that-does-not-exist")
        assert "habit does not exist" in str(excinfo.value).lower()


async def test_get_areas(client, mock_async_response, patch_client_method):
    mock_resp = mock_async_response("get_areas.yaml")

    with patch_client_method("get", return_value=mock_resp) as mock_get:
        areas = await client.get_areas()

        mock_get.assert_called_once_with("/areas")
        assert_that(areas, all_of(has_length(greater_than(0)), only_contains(instance_of(Area))))
        assert areas[0].id == "-LrYlUBnzjyceYei_k5Z"
        assert areas[0].name == "H****h"


async def test_get_journal(client, mock_async_response, patch_client_method):
    today = datetime.date.today().isoformat()
    mock_resp = mock_async_response("get_journal.yaml")

    with patch_client_method("get", return_value=mock_resp) as mock_get:
        habits = await client.get_journal(date=today)

        mock_get.assert_called_once()
        url = mock_get.call_args[0][0]
        params = mock_get.call_args[1]["params"]

        assert url == "/journal"
        assert "target_date" in params
        assert params["order_by"] == "priority"
        assert_that(habits, only_contains(instance_of(Habit)))


async def test_get_journal_filtered(client, mock_async_response, patch_client_method):
    today = datetime.date.today().isoformat()
    mock_resp = mock_async_response("get_journal_filtered.yaml")

    with patch_client_method("get", return_value=mock_resp) as mock_get:
        await client.get_journal(date=today, status="none", time_of_day="morning,evening")

        mock_get.assert_called_once()
        url = mock_get.call_args[0][0]
        params = mock_get.call_args[1]["params"]

        assert url == "/journal"
        assert "target_date" in params
        assert params["status"] == "none"
        assert params["time_of_day"] == "morning,evening"


async def test_check_habit_status(client, mock_async_response, patch_client_method):
    mock_resp = mock_async_response("get_habit_status.yaml")

    with patch_client_method("get", return_value=mock_resp) as mock_get:
        status = await client.check_habit_status("-Lo9NTLRX3aCxg-PjN25", date="2025-05-09")

        mock_get.assert_called_once()
        url = mock_get.call_args[0][0]
        params = mock_get.call_args[1]["params"]

        assert url == "/status/-Lo9NTLRX3aCxg-PjN25"
        assert "target_date" in params
        assert_that(status, instance_of(HabitStatus))
        assert status.status == Status.COMPLETED


async def test_check_habit_status_invalid_date(client, mock_async_response, patch_client_method):
    mock_resp = mock_async_response("get_habit_status_(invalid_date_format).yaml", status_code=500)
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "HTTP Error", request=MagicMock(), response=mock_resp
    )

    with patch_client_method("get", return_value=mock_resp) as mock_get:
        with pytest.raises(HabitifyError) as excinfo:
            await client.check_habit_status("-Lo9NTLRX3aCxg-PjN25", date="2020-01-01")

        mock_get.assert_called_once()
        assert "date format" in str(excinfo.value).lower()


async def test_set_habit_status(client, mock_async_response, patch_client_method):
    mock_resp = mock_async_response("set_habit_status_(completed).yaml")

    with patch_client_method("put", return_value=mock_resp) as mock_put:
        status = await client.set_habit_status(
            "-Lo9NTLRX3aCxg-PjN25",
            status=Status.COMPLETED,
            date="2025-05-09",
            note="Test completed via async unit test",
            value=1.0,
        )

        mock_put.assert_called_once()
        url = mock_put.call_args[0][0]
        body = mock_put.call_args[1]["json"]

        assert url == "/status/-Lo9NTLRX3aCxg-PjN25"
        assert body["status"] == "completed"
        assert "target_date" in body
        assert body["note"] == "Test completed via async unit test"
        assert body["value"] == 1.0
        assert_that(status, instance_of(HabitStatus))
        assert_that(
            status, has_properties(status=Status.COMPLETED, note="Test completed via async unit test", value=1.0)
        )


async def test_set_habit_status_skipped(client, mock_async_response, patch_client_method):
    mock_resp = mock_async_response("set_habit_status_(skipped).yaml")

    with patch_client_method("put", return_value=mock_resp) as mock_put:
        status = await client.set_habit_status(
            "-Lo9NTLRX3aCxg-PjN25", status=Status.SKIPPED, date="2025-05-09", note="Test skipped via async unit test"
        )

        mock_put.assert_called_once()
        url = mock_put.call_args[0][0]
        body = mock_put.call_args[1]["json"]

        assert url == "/status/-Lo9NTLRX3aCxg-PjN25"
        assert body["status"] == "skipped"
        assert "target_date" in body
        assert body["note"] == "Test skipped via async unit test"
        assert "value" not in body
        assert_that(status, instance_of(HabitStatus))
        assert_that(status, has_properties(status=Status.SKIPPED, note="Test skipped via async unit test", value=None))


if __name__ == "__main__":
    pytest_bazel.main()
