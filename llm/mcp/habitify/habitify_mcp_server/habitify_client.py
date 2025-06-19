"""
Habitify API client for interacting with the Habitify API.

Implements only the endpoints shown in the API reference YAML files.
"""

import asyncio
import datetime
import logging
import os
from typing import Any, Literal, Optional
import httpx
from dotenv import load_dotenv

from .types import Area, Habit, HabitStatus, HabitStatusResponse
from .utils.date_utils import (
    create_date_range,
    format_date_for_api,
    format_date_yyyy_mm_dd,
)

logger = logging.getLogger("habitify.client")

# Load environment variables
load_dotenv()


class HabitifyError(Exception):
    """Custom exception for Habitify API errors."""

    status_code: Optional[int] = None

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class HabitifyClient:
    """
    Client for the Habitify API.

    Supports only the endpoints documented in the reference YAML files.
    """

    def __init__(self, api_key: Optional[str] = None, timeout: float = 10.0):
        """
        Initialize the Habitify API client.

        Args:
            api_key: API key for the Habitify API. If not provided, will use HABITIFY_API_KEY env var.
            timeout: Timeout for API requests in seconds (default: 10.0).
        """
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

        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout,
        )

    async def __aenter__(self) -> "HabitifyClient":
        """Support async context manager protocol."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Close resources when exiting async context manager."""
        await self.client.aclose()

    def _process_response(self, response: httpx.Response, model_class=None) -> Any:
        """
        Process an HTTP response and convert it to the appropriate type.

        Args:
            response: HTTP response
            model_class: Pydantic model class to use for conversion

        Returns:
            Processed response data
        """
        data = response.json()
        result = data.get("data", data)

        if model_class:
            if isinstance(result, list):
                return [model_class(**item) for item in result]
            elif result is None:
                # For some endpoints that return null data but success
                if "status" in data and data["status"] is True:
                    status_obj = HabitStatus(status="success")
                    return status_obj
                return None
            else:
                return model_class(**result)

        return result

    def _validate_habit_id(self, habit_id: str) -> str:
        """
        Simply validate that habit ID is not empty.

        Args:
            habit_id: Habit ID as provided

        Returns:
            The same habit ID unchanged
        """
        if not habit_id:
            raise HabitifyError("Habit ID is required")

        return habit_id

    #
    # Documented API endpoints based on API reference YAML files
    #

    async def get_habits(self) -> list[Habit]:
        """
        Get all habits.

        Endpoint: GET /habits

        Returns:
            List of habits as Pydantic models
        """
        try:
            response = await self.client.get("/habits")
            response.raise_for_status()
            return self._process_response(response, Habit)
        except Exception as e:
            raise self._handle_error(e)

    async def get_habit(self, habit_id: str) -> Habit:
        """
        Get a single habit by ID.

        Endpoint: GET /habits/{habit_id}

        Args:
            habit_id: The habit ID

        Returns:
            Habit details as a Pydantic model
        """
        habit_id = self._validate_habit_id(habit_id)

        try:
            response = await self.client.get(f"/habits/{habit_id}")
            response.raise_for_status()
            return self._process_response(response, Habit)
        except Exception as e:
            raise self._handle_error(e)

    async def get_areas(self) -> list[Area]:
        """
        Get all habit areas/categories.

        Endpoint: GET /areas

        Returns:
            List of areas as Pydantic models
        """
        try:
            response = await self.client.get("/areas")
            response.raise_for_status()
            return self._process_response(response, Area)
        except Exception as e:
            raise self._handle_error(e)

    async def get_journal(
        self,
        date: Optional[datetime.date] = None,
        order_by: Optional[str] = "priority",
        status: Optional[str] = None,
        time_of_day: Optional[str] = None,
        area_id: Optional[str] = None,
    ) -> list[Habit]:
        """
        Get filtered habits for a specific date.

        Endpoint: GET /journal

        Args:
            date: Date to filter habits for (required, defaults to today if None)
            order_by: How to order habits (priority, reminder_time, status)
            status: Filter by status (comma-separated: none, in_progress, completed, failed, skipped)
            time_of_day: Filter by time (comma-separated: morning, afternoon, evening, any_time)
            area_id: Filter by specific area/category ID

        Returns:
            List of habits for the specified date
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
            return self._process_response(response, Habit)
        except Exception as e:
            raise self._handle_error(e)

    # All methods are async-only now

    async def check_habit_status(
        self, habit_id: str, date: Optional[str | datetime.date] = None
    ) -> HabitStatusResponse:
        """
        Check a habit's status for a date.

        Endpoint: GET /status/{habit_id}

        Args:
            habit_id: The habit ID
            date: Optional date in YYYY-MM-DD format or date object (defaults to today)

        Returns:
            Habit status as a client response model with Python date object
        """
        habit_id = self._validate_habit_id(habit_id)
        check_date = format_date_for_api(date)

        try:
            response = await self.client.get(
                f"/status/{habit_id}",
                params={
                    "target_date": check_date,
                },
            )
            response.raise_for_status()
            api_result = self._process_response(response, HabitStatus)

            # If API didn't return a date, add the request date to the API model
            if not api_result.date:
                api_result.date = (
                    format_date_yyyy_mm_dd(date)
                    if date
                    else datetime.date.today().isoformat()
                )

            # Convert the API model to a client response model with Python date object
            return HabitStatusResponse.from_api_model(api_result, date)
        except Exception as e:
            raise self._handle_error(e)

    async def check_habit_status_range(
        self,
        habit_id: str,
        start_date: Optional[str | datetime.date] = None,
        end_date: Optional[str | datetime.date] = None,
        days: Optional[int] = None,
    ) -> list[HabitStatusResponse]:
        """
        Check a habit's status for a range of dates.

        This is a client-side implementation that fetches individual status records
        for each date in the range concurrently.

        Args:
            habit_id: The habit ID
            start_date: Start date of the range (inclusive)
            end_date: End date of the range (inclusive)
            days: Number of days to include

        Returns:
            List of habit status client response models with Python date objects, one for each date in the range
        """
        habit_id = self._validate_habit_id(habit_id)

        # Create the date range
        start_dt, end_dt, date_range = create_date_range(
            start_date=start_date, end_date=end_date, days=days
        )

        # Prepare coroutines for each date
        tasks = []
        date_map = {}  # Map to track which date corresponds to which task

        # Create all the coroutines
        for date in date_range:
            formatted_date = format_date_yyyy_mm_dd(date)
            task = asyncio.create_task(self.check_habit_status(habit_id, date))
            tasks.append(task)
            date_map[task] = formatted_date

        # Run all tasks concurrently
        try:
            # Wait for all tasks to complete, getting results in completion order
            results = await asyncio.gather(*tasks)
        except Exception as e:
            # Cancel any pending tasks if one fails
            for task in tasks:
                if not task.done():
                    task.cancel()
            logger.error(f"Error fetching habit status: {str(e)}")
            raise

        # Sort results by date to maintain chronological order
        results.sort(key=lambda x: x.date)

        return results

    # All methods are async-only now

    async def set_habit_status(
        self,
        habit_id: str,
        status: Literal["completed", "skipped", "failed", "none"],
        date: Optional[str | datetime.date] = None,
        note: Optional[str] = None,
        value: Optional[float] = None,
    ) -> HabitStatusResponse:
        """
        Set a habit's status for a specific date.

        Endpoint: PUT /status/{habit_id}

        Args:
            habit_id: The habit ID
            status: Status to set ('completed', 'skipped', 'failed', 'none')
            date: Optional date in YYYY-MM-DD format or date object (defaults to today)
            note: Optional note to attach to the log
            value: Optional value for habits with goals

        Returns:
            Client response model with Python date object
        """
        if not status:
            raise HabitifyError("Status is required")

        habit_id = self._validate_habit_id(habit_id)
        target_date = format_date_for_api(date)

        # Build the request body based on examples
        request_body = {
            "status": status,
            "target_date": target_date,
        }

        # Add optional parameters if provided
        if note is not None:
            request_body["note"] = note

        if value is not None:
            request_body["value"] = value

        try:
            response = await self.client.put(f"/status/{habit_id}", json=request_body)
            response.raise_for_status()

            # Create API result model with the input data since the API returns null for success
            api_result = HabitStatus(
                status=status,
                date=(
                    format_date_yyyy_mm_dd(date)
                    if date
                    else datetime.date.today().isoformat()
                ),
                note=note,
                value=value,
            )

            # Convert to client response model with Python date object
            return HabitStatusResponse.from_api_model(api_result, date)
        except Exception as e:
            raise self._handle_error(e)

    def _handle_error(self, error: Exception) -> HabitifyError:
        """
        Handle API errors based on observed error patterns in examples.

        Args:
            error: The error to handle

        Returns:
            A more descriptive error
        """
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
                return HabitifyError(
                    "Authentication failed. Please check your Habitify API key.", status
                )
            elif status == 404:
                return HabitifyError(
                    "Resource not found. This endpoint may not be supported by the API.",
                    status,
                )
            elif status == 500 and data and "message" in data:
                if "habit does not exist" in data["message"].lower():
                    return HabitifyError(
                        f"Habit ID not found: {data['message']}", status
                    )
                elif "target_date" in data["message"].lower():
                    return HabitifyError(
                        "Invalid date format. The API requires ISO 8601 format (YYYY-MM-DDThh:mm:ss±hh:mm).",
                        status,
                    )
                else:
                    return HabitifyError(f"API Error: {data['message']}", status)

            # Create a readable error message with available details
            error_prefix = f"HTTP {status}:"
            if data and "message" in data:
                return HabitifyError(f"{error_prefix} {data['message']}", status)
            elif data:
                return HabitifyError(f"{error_prefix} {data}", status)

            return HabitifyError(f"{error_prefix} Request failed", status)

        # For network errors or other non-API errors
        return HabitifyError(f"Connection error: {str(error)}")
