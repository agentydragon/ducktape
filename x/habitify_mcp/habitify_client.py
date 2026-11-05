"""Habitify client limited to the endpoints recorded in api_reference/*.yaml."""

from __future__ import annotations

import datetime
import logging
import os
from typing import Any, cast

import httpx
from dotenv import load_dotenv

from x.habitify_mcp.types import Area, Habit, HabitStatus, Status
from x.habitify_mcp.utils.date_utils import format_date_for_api

logger = logging.getLogger("habitify.client")

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

    def _process_response(self, response: httpx.Response, model_class: type | None = None) -> Any:
        data = response.json()
        result = data.get("data", data)

        if model_class:
            if isinstance(result, list):
                return [model_class(**item) for item in result]
            if result is None:
                # For some endpoints that return null data but success - just return None
                # since we don't have actual status data to construct a model
                return None
            return model_class(**result)

        return result

    def _validate_habit_id(self, habit_id: str) -> str:
        if not habit_id:
            raise HabitifyError("Habit ID is required")

        return habit_id

    #
    # Documented API endpoints based on API reference YAML files
    #

    async def get_habits(self) -> list[Habit]:
        """GET /habits: list habits."""
        try:
            response = await self.client.get("/habits")
            response.raise_for_status()
            return cast(list[Habit], self._process_response(response, Habit))
        except Exception as e:
            raise self._handle_error(e)

    async def get_habit(self, habit_id: str) -> Habit:
        """GET /habits/{habit_id}: fetch one habit."""
        habit_id = self._validate_habit_id(habit_id)

        try:
            response = await self.client.get(f"/habits/{habit_id}")
            response.raise_for_status()
            return cast(Habit, self._process_response(response, Habit))
        except Exception as e:
            raise self._handle_error(e)

    async def get_areas(self) -> list[Area]:
        """GET /areas: list habit areas/categories."""
        try:
            response = await self.client.get("/areas")
            response.raise_for_status()
            return cast(list[Area], self._process_response(response, Area))
        except Exception as e:
            raise self._handle_error(e)

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
        target_date = format_date_for_api(date)

        # Build query parameters
        params = {"target_date": target_date}

        if order_by:
            params["order_by"] = order_by

        if status:
            params["status"] = status

        if time_of_day:
            params["time_of_day"] = time_of_day

        if area_id:
            params["area_id"] = area_id

        try:
            response = await self.client.get("/journal", params=params)
            response.raise_for_status()
            return cast(list[Habit], self._process_response(response, Habit))
        except Exception as e:
            raise self._handle_error(e)

    async def check_habit_status(
        self, habit_id: str, date: str | datetime.date | datetime.datetime | None = None
    ) -> HabitStatus:
        """GET /status/{habit_id}: return status with a timezone-aware date (today by default)."""
        habit_id = self._validate_habit_id(habit_id)
        check_date = format_date_for_api(date)

        try:
            response = await self.client.get(f"/status/{habit_id}", params={"target_date": check_date})
            response.raise_for_status()
            result = cast(HabitStatus, self._process_response(response, HabitStatus))

            # If API didn't return a date, add the request date as timezone-aware datetime
            if not result.date:
                if isinstance(date, datetime.datetime):
                    result.date = date if date.tzinfo else date.replace(tzinfo=datetime.UTC)
                elif isinstance(date, datetime.date):
                    result.date = datetime.datetime.combine(date, datetime.time.min, tzinfo=datetime.UTC)
                elif isinstance(date, str):
                    # Parse string to datetime
                    parsed = datetime.datetime.fromisoformat(date)
                    result.date = parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)
                else:
                    result.date = datetime.datetime.now(tz=datetime.UTC)

            return result
        except Exception as e:
            raise self._handle_error(e)

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
        target_date = format_date_for_api(date)

        # Build the request body based on examples (API expects string value)
        # Status is a str-based enum, so it can be used directly as a string
        request_body: dict[str, Any] = {"status": status, "target_date": target_date}

        # Add optional parameters if provided
        if note is not None:
            request_body["note"] = note

        if value is not None:
            request_body["value"] = value

        try:
            response = await self.client.put(f"/status/{habit_id}", json=request_body)
            response.raise_for_status()

            # Create result model with the input data since the API returns null for success
            # Convert to timezone-aware datetime
            if isinstance(date, datetime.datetime):
                result_date = date if date.tzinfo else date.replace(tzinfo=datetime.UTC)
            elif isinstance(date, datetime.date):
                result_date = datetime.datetime.combine(date, datetime.time.min, tzinfo=datetime.UTC)
            elif isinstance(date, str):
                parsed = datetime.datetime.fromisoformat(date)
                result_date = parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)
            else:
                result_date = datetime.datetime.now(tz=datetime.UTC)

            return HabitStatus(status=status, date=result_date, note=note, value=value)
        except Exception as e:
            raise self._handle_error(e)

    def _handle_error(self, error: Exception) -> HabitifyError:
        if isinstance(error, httpx.HTTPStatusError):
            response = error.response
            status = response.status_code

            # Try to parse response JSON, log but continue if it fails
            data = None
            try:
                data = response.json()
            except Exception as json_error:
                # Instead of silently setting data to None, log what happened
                logger.warning(
                    f"Failed to parse error response JSON: {json_error}. Response text: {response.text[:100]}..."
                )

            # Check for common errors with helpful messages
            if status == 401:
                return HabitifyError("Authentication failed. Please check your Habitify API key.", status)
            if status == 404:
                return HabitifyError("Resource not found. This endpoint may not be supported by the API.", status)
            if status == 500 and data and "message" in data:
                if "habit does not exist" in data["message"].lower():
                    return HabitifyError(f"Habit ID not found: {data['message']}", status)
                if "target_date" in data["message"].lower():
                    return HabitifyError(
                        "Invalid date format. The API requires ISO 8601 format (YYYY-MM-DDThh:mm:ss±hh:mm).", status
                    )
                return HabitifyError(f"API Error: {data['message']}", status)

            # Create a readable error message with available details
            error_prefix = f"HTTP {status}:"
            if data and "message" in data:
                return HabitifyError(f"{error_prefix} {data['message']}", status)
            if data:
                return HabitifyError(f"{error_prefix} {data}", status)

            return HabitifyError(f"{error_prefix} Request failed", status)

        # For network errors or other non-API errors
        return HabitifyError(f"Connection error: {error!s}")
