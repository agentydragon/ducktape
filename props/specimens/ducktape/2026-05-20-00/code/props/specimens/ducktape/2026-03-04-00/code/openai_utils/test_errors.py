"""Unit tests for openai_utils.errors."""

from __future__ import annotations

import pytest
import pytest_bazel
from openai import BadRequestError

from openai_utils.model import ResponsesRequest
from openai_utils.testing.fixtures import error_transport, mock_openai_client


async def test_non_context_length_bad_request_propagates() -> None:
    """A BadRequestError with a non-context-length code propagates unchanged."""
    client = mock_openai_client(error_transport("invalid_request_error", "invalid request"))

    with pytest.raises(BadRequestError):
        await client.responses_create(ResponsesRequest(input="hi", max_output_tokens=16))


if __name__ == "__main__":
    pytest_bazel.main()
