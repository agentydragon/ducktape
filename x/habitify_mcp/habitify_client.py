"""Habitify client limited to the endpoints recorded in api_reference/*.yaml."""

from __future__ import annotations

import datetime
import os
from collections.abc import Awaitable
from typing import Any

import httpx
from dotenv import load_dotenv
from pydantic import TypeAdapter

from x.habitify_mcp.types import Area, Habit, HabitStatus, Status
from x.habitify_mcp.utils.date_utils import target_datetime

# Load environment variables
load_dotenv()


class HabitifyError(Exception):
    status_code: int | None = None

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class HabitifyClient:
    def __init__(self, api_key: str | None = None, timeout: float = 10.0):
        """Use HABITIFY_API_KEY when api_key is absent; timeout is in seconds."""
        # Priority order: passed api_key param, HABITIFY_API_KEY env var
        self.api_key = api_key or os.getenv("HABITIFY_API_KEY")
        self.base_url = os.getenv("HABITIFY_API_BASE_URL", "https://api.habitify.me")

        if not self.api_key:
            raise HabitifyError(
                "Habitify API key is required. Set HABITIFY_API_KEY environment variable or pass to constructor."
            )

        headers = {
            "Authorization": self.api_key,  # No 'Bearer' prefix based on examples
            "Content-Type": "application/json",
        }

        # Store timeout for creating clients
        self.timeout = timeout

        self.client = httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=self.timeout)

    async def __aenter__(self) -> HabitifyClient:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.client.aclose()

    async def _get[T](self, path: str, model: TypeAdapter[T], params: dict[str, str] | None = None) -> T:
        request = self.client.get(path, params=params) if params is not None else self.client.get(path)
        response = await _checked(request)
        try:
            body = response.json()
            return model.validate_python(body.get("data", body) if isinstance(body, dict) else body)
        except ValueError as error:
            raise HabitifyError(f"Invalid Habitify response for {path}", response.status_code) from error

    def _validate_habit_id(self, habit_id: str) -> str:
        if not habit_id:
            raise HabitifyError("Habit ID is required")

        return habit_id

    #
    # Documented API endpoints based on API reference YAML files
    #

    async def get_habits(self) -> list[Habit]:
        """GET /habits: list habits."""
        return await self._get("/habits", TypeAdapter(list[Habit]))

    async def get_habit(self, habit_id: str) -> Habit:
        """GET /habits/{habit_id}: fetch one habit."""
        habit_id = self._validate_habit_id(habit_id)

        return await self._get(f"/habits/{habit_id}", TypeAdapter(Habit))

    async def get_areas(self) -> list[Area]:
        """GET /areas: list habit areas/categories."""
        return await self._get("/areas", TypeAdapter(list[Area]))

    async def get_journal(
        self,
        date: datetime.date | None = None,
        order_by: str | None = "priority",
        status: Status | None = None,
        time_of_day: str | None = None,
        area_id: str | None = None,
    ) -> list[Habit]:
        """GET /journal: filter habits for a date (today by default).

        Filter/order values: api_reference/API_REFERENCE.md.
        Request examples: api_reference/get_journal{,_filtered}.yaml.
        """
        target_date = target_datetime(date)

        # Build query parameters
        params = {"target_date": target_date.isoformat()}

        if order_by:
            params["order_by"] = order_by

        if status:
            params["status"] = status

        if time_of_day:
            params["time_of_day"] = time_of_day

        if area_id:
            params["area_id"] = area_id

        return await self._get("/journal", TypeAdapter(list[Habit]), params=params)

    async def check_habit_status(
        self, habit_id: str, date: str | datetime.date | datetime.datetime | None = None
    ) -> HabitStatus:
        """GET /status/{habit_id}: return status with a timezone-aware date (today by default)."""
        habit_id = self._validate_habit_id(habit_id)
        check_date = target_datetime(date)

        result = await self._get(
            f"/status/{habit_id}", TypeAdapter(HabitStatus), params={"target_date": check_date.isoformat()}
        )
        if result.date is None:
            result.date = check_date
        return result

    async def set_habit_status(
        self,
        habit_id: str,
        status: Status,
        date: str | datetime.date | datetime.datetime | None = None,
        note: str | None = None,
        value: float | None = None,
    ) -> HabitStatus:
        """PUT /status/{habit_id}: synthesize timezone-aware status after the API returns null on success."""
        if not status:
            raise HabitifyError("Status is required")

        habit_id = self._validate_habit_id(habit_id)
        target_date = target_datetime(date)

        # Build the request body based on examples (API expects string value)
        # Status is a str-based enum, so it can be used directly as a string
        request_body: dict[str, Any] = {"status": status, "target_date": target_date.isoformat()}

        # Add optional parameters if provided
        if note is not None:
            request_body["note"] = note

        if value is not None:
            request_body["value"] = value

        await _checked(self.client.put(f"/status/{habit_id}", json=request_body))
        return HabitStatus(status=status, date=target_date, note=note, value=value)


async def _checked(request: Awaitable[httpx.Response]) -> httpx.Response:
    try:
        response = await request
        response.raise_for_status()
        return response
    except httpx.HTTPStatusError as error:
        raise _status_error(error.response) from error
    except httpx.RequestError as error:
        raise HabitifyError(f"Connection error: {error}") from error


def _status_error(response: httpx.Response) -> HabitifyError:
    status = response.status_code
    if status == 401:
        return HabitifyError("Authentication failed. Please check your Habitify API key.", status)
    if status == 404:
        return HabitifyError("Resource not found. This endpoint may not be supported by the API.", status)
    try:
        data = response.json()
    except ValueError:
        data = None
    message = data.get("message") if isinstance(data, dict) else None
    if status == 500 and isinstance(message, str):
        if "habit does not exist" in message.lower():
            return HabitifyError(f"Habit ID not found: {message}", status)
        if "target_date" in message.lower():
            return HabitifyError(
                "Invalid date format. The API requires ISO 8601 format (YYYY-MM-DDThh:mm:ss±hh:mm).", status
            )
        return HabitifyError(f"API Error: {message}", status)
    return HabitifyError(f"HTTP {status}: {message or data or 'Request failed'}", status)
