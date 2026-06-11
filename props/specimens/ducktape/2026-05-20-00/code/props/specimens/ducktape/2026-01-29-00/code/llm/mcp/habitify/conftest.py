"""Shared pytest configuration and fixtures for habitify tests."""

import json
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import yaml

from habitify.habitify_client import HabitifyClient
from habitify.testing_models import TestReference


def pytest_configure(config):
    """Configure pytest-asyncio to auto mode."""
    config.addinivalue_line("markers", "asyncio: mark test as async")
    config.option.asyncio_mode = "auto"


# Reference data is in the api_reference directory
_REFERENCE_DATA_DIR = Path(__file__).parent / "api_reference"


@pytest.fixture
def load_reference_data():
    """Factory fixture for loading typed reference data.

    Returns a function that loads YAML reference files and validates them
    against the TestReference Pydantic model.
    """

    def _load(filename: str) -> TestReference:
        ref_file = _REFERENCE_DATA_DIR / filename
        return TestReference.model_validate(yaml.safe_load(ref_file.read_text()))

    return _load


@pytest.fixture
def client():
    """Create a Habitify client with a mock API key."""
    with patch.dict(os.environ, {"HABITIFY_API_KEY": "test_api_key"}):
        client = HabitifyClient()
        yield client


@pytest.fixture
def mock_async_response(load_reference_data):
    """Create a mock async response factory for async HTTP responses."""

    def _create_mock_async_response(filename: str, status_code: int = 200):
        """Create a mock async response from a reference file."""
        ref_data = load_reference_data(filename)

        mock_resp = AsyncMock(spec=httpx.Response)
        mock_resp.status_code = status_code
        mock_resp.raise_for_status.return_value = None
        mock_resp.headers = ref_data.response.headers

        # Set response content
        if ref_data.response.json_data is not None:
            mock_resp.json.return_value = ref_data.response.json_data
        elif ref_data.response.text is not None:
            mock_resp.text = ref_data.response.text
            mock_resp.json.side_effect = json.JSONDecodeError("", "", 0)

        return mock_resp

    return _create_mock_async_response


@pytest.fixture
def patch_client_method(client):
    """Fixture that patches client HTTP methods with consistent API."""

    @contextmanager
    def _patch(method_name, return_value=None, side_effect=None):
        with patch.object(
            client.client, method_name, return_value=return_value, side_effect=side_effect
        ) as mock_method:
            yield mock_method

    return _patch
