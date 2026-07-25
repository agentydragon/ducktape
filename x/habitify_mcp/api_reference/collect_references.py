#!/usr/bin/env python3
"""
Script to collect Habitify API reference examples.

This script makes calls to all the Habitify API endpoints used in the MCP server
and saves the requests and responses as reference examples in YAML format.
"""

import dataclasses
import logging
import os
import sys
from functools import cached_property
from pathlib import Path
from typing import Any

import httpx
import yaml

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("habitify_api_reference")

# Directory to save reference files
REFERENCE_DIR = Path(__file__).parent.resolve()


@dataclasses.dataclass
class ApiReferenceCollector:
    """Collects API requests and responses for reference documentation."""

    api_key: str
    base_url: str = "https://api.habitify.me"

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": self.api_key, "Content-Type": "application/json"}

    @property
    def masked_headers(self) -> dict[str, str]:
        """Return headers with API key properly masked."""
        return {"Authorization": "API_KEY_MASKED", "Content-Type": "application/json"}

    @cached_property
    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, headers=self.headers, timeout=10.0)

    def _filter_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """
        Filter HTTP headers to only keep useful ones.

        Args:
            headers: Original HTTP headers

        Returns:
            Filtered headers containing only useful information
        """
        headers_to_exclude = [
            "accept-ranges",
            "access-control-allow-origin",
            "alt-svc",
            "cache-control",
            "connection",
            "content-length",
            "date",
            "etag",
            "fastly-restarts",
            "server",
            "strict-transport-security",
            "vary",
            "x-cache",
            "x-cache-hits",
            "x-cloud-trace-context",
            "x-country-code",
            "x-powered-by",
            "x-served-by",
            "x-timer",
        ]
        return {name: value for name, value in headers.items() if name.lower() not in headers_to_exclude}

    def _mask_name(self, name: str) -> str:
        """
        Mask a habit name for logging and display.

        Args:
            name: The habit name to mask

        Returns:
            Masked name with first and last characters visible
        """
        if not name or len(name) <= 2:
            return name

        # Keep first character and replace rest with asterisks
        # but keep the last character if the string is long enough
        if len(name) <= 4:
            return name[0] + "*" * (len(name) - 1)
        return name[0] + "*" * (len(name) - 2) + name[-1]

    def _mask_sensitive_data(self, data: Any) -> Any:
        """
        Mask sensitive data in API responses and requests.

        This function recursively processes dictionaries, lists, and other data structures
        to mask sensitive information like habit names and API keys.

        Args:
            data: The data to mask

        Returns:
            The masked data
        """
        # Handle different data types
        if isinstance(data, dict):
            # Process dictionaries recursively
            result = {}
            for key, value in data.items():
                # Special handling for sensitive keys
                if key in ("Authorization", "api_key"):
                    result[key] = "API_KEY_MASKED"
                # Special handling for habit names
                elif key == "name" and isinstance(value, str):
                    result[key] = self._mask_name(value)
                else:
                    # Recursively process the value
                    result[key] = self._mask_sensitive_data(value)
            return result
        if isinstance(data, list):
            # Process lists recursively
            return [self._mask_sensitive_data(item) for item in data]
        # Return primitive values unchanged
        return data

    def _make_request_and_save(
        self,
        name: str,
        method: str,
        endpoint: str,
        expected_status: int,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Make a request to the API, validate response, and save as reference.

        Args:
            name: Name of the reference
            method: HTTP method
            endpoint: API endpoint
            expected_status: Expected HTTP status code (optional)
            params: Request query parameters (optional)
            json_data: Request body (optional)

        Returns:
            Dict containing the response data

        Raises:
            SystemExit: If expected_status is specified and doesn't match actual status
        """
        logger.info(f"Making request: {name} ({method} {endpoint})")

        response = self.client.request(method=method, url=endpoint, params=params, json=json_data)

        # If expected status is provided, validate it
        if response.status_code != expected_status:
            logger.error(f"Expected status {expected_status} but got {response.status_code}")
            sys.exit(1)

        # Create reference data structure
        reference = {
            "name": name,
            "request": {"method": method, "url": f"{self.base_url}{endpoint}", "headers": self.masked_headers},
            "response": {
                "status_code": response.status_code,
                # Filter headers to only include useful ones
                "headers": self._filter_headers(response.headers),
                "json": self._mask_sensitive_data(response.json()),
            },
        }
        # Mask any sensitive data in request params and body
        if params:
            reference["request"]["params"] = self._mask_sensitive_data(params)

        if json_data:
            reference["request"]["body"] = self._mask_sensitive_data(json_data)

        # Save to file in YAML format
        path = REFERENCE_DIR / f"{name.lower().replace(' ', '_')}.yaml"
        if path.exists():
            logger.warning(f"Overwriting existing file: {path}")

        with path.open("w") as f:
            yaml.dump(reference, f, sort_keys=False, indent=2, default_flow_style=False)

        logger.info(f"Saved reference example to {path}")

        return response.json()

    def _clear_existing_references(self) -> None:
        """Delete all existing YAML reference files in the reference directory."""
        for file_path in list(REFERENCE_DIR.glob("*.yaml")):
            file_path.unlink()

    def collect_references(self) -> None:
        """Collect reference examples for all API endpoints."""
        self._clear_existing_references()

        logger.info("Collecting Habitify API reference examples.")

        # Part 1: Success examples for core endpoints
        # -------------------------------------------

        # Get list of habits - should always work
        response_data = self._make_request_and_save(
            name="Get Habits", method="GET", endpoint="/habits", expected_status=200
        )
        habits = response_data["data"]

        # Fail fast if no habits found
        if not habits:
            logger.error("No habits found in account - aborting")
            sys.exit(1)

        # Use the first habit for further API calls
        habit = habits[0]
        habit_id = habit["id"]
        logger.info(f"Using habit with ID: {habit_id} and masked name: {self._mask_name(habit['name'])}")

        # Get details for a specific habit by ID
        self._make_request_and_save(
            name="Get Habit by ID", method="GET", endpoint=f"/habits/{habit_id}", expected_status=200
        )

        # Correct format - ISO-8601 with +00:00 (YYYY-MM-DDT00:00:00+00:00)
        valid_date = "2025-05-09T00:00:00+00:00"
        self._make_request_and_save(
            name="Get Habit Status",
            method="GET",
            endpoint=f"/status/{habit_id}",
            params={"target_date": valid_date},
            expected_status=200,
        )

        # Set habit status with +00:00 format (mark as completed)
        self._make_request_and_save(
            name="Set Habit Status (Completed)",
            method="PUT",
            endpoint=f"/status/{habit_id}",
            json_data={
                "status": "completed",
                "target_date": valid_date,
                "note": "Completed via API reference collector",
                "value": 1.0,
            },
            expected_status=200,
        )

        # Set habit status with +00:00 format (mark as skipped)
        self._make_request_and_save(
            name="Set Habit Status (Skipped)",
            method="PUT",
            endpoint=f"/status/{habit_id}",
            json_data={"status": "skipped", "target_date": valid_date, "note": "Skipped via API reference collector"},
            expected_status=200,
        )

        # Set habit status with +00:00 format (mark as failed)
        self._make_request_and_save(
            name="Set Habit Status (Failed)",
            method="PUT",
            endpoint=f"/status/{habit_id}",
            json_data={"status": "failed", "target_date": valid_date, "note": "Failed via API reference collector"},
            expected_status=200,
        )

        # Set habit status with +00:00 format (mark as completed without value)
        # This is for habits that don't have a value, just a status
        self._make_request_and_save(
            name="Set Habit Status (No Value)",
            method="PUT",
            endpoint=f"/status/{habit_id}",
            json_data={
                "status": "completed",
                "target_date": valid_date,
                "note": "Completed via API reference collector (no value)",
            },
            expected_status=200,
        )

        # Get all areas (categories)
        self._make_request_and_save(name="Get Areas", method="GET", endpoint="/areas", expected_status=200)

        # Get journal with proper date format and filtering options
        # According to docs, the journal endpoint supports: target_date, order_by, status, area_id, time_of_day
        self._make_request_and_save(
            name="Get Journal",
            method="GET",
            endpoint="/journal",
            params={"target_date": valid_date, "order_by": "priority"},
            expected_status=200,
        )

        # Documented endpoints from official API documentation (docs.habitify.me)
        # ---------------------------------------------------------

        # Try journal with additional filter parameters (documented in API)
        self._make_request_and_save(
            name="Get Journal Filtered",
            method="GET",
            endpoint="/journal",
            params={
                "target_date": valid_date,
                "order_by": "priority",
                "time_of_day": "morning,evening",
                "status": "none",
            },
            expected_status=200,
        )

        # Part 2: Error cases and edge cases
        # ---------------------------------------------------------

        # Non-existent habit ID for GET /habits/{id}
        self._make_request_and_save(
            name="Get Habit Invalid ID",
            method="GET",
            endpoint="/habits/invalid-id-that-does-not-exist",
            expected_status=500,
        )

        # Non-existent habit ID for GET /status/{id}
        self._make_request_and_save(
            name="Get Status Invalid ID",
            method="GET",
            endpoint="/status/invalid-id-that-does-not-exist",
            params={"target_date": valid_date},
            expected_status=500,
        )

        # Incorrect date format example
        self._make_request_and_save(
            name="Get Habit Status (Invalid Date Format)",
            method="GET",
            endpoint=f"/status/{habit_id}",
            params={"target_date": "2020-01-01"},
            expected_status=500,
        )

        logger.info("API reference examples collected")


def main():
    """Main entry point for the script."""
    # Get API key from environment
    api_key = os.environ.get("HABITIFY_API_KEY")
    if not api_key:
        logger.error("ERROR: HABITIFY_API_KEY environment variable is not set.")
        sys.exit(1)

    collector = ApiReferenceCollector(api_key)
    collector.collect_references()

    logger.info("Finished collecting API reference examples.")


if __name__ == "__main__":
    main()
