"""Assertion and extraction helpers for test validation.

Provides utilities for verifying tool calls and extracting structured outputs from
MCP tool results in tests.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import TypeGuard

import pytest
from pydantic import BaseModel

from agent_core.agent import _openai_to_mcp_result
from mcp_infra.calltool import extract_structured_content
from openai_utils.model import FunctionCallItem, FunctionCallOutputItem, ResponsesRequest, SystemMessage, UserMessage

logger = logging.getLogger(__name__)


def get_last_function_output[T: BaseModel](req: ResponsesRequest, output_type: type[T]) -> T:
    """Extract and parse structured content from last FunctionCallOutputItem in request input.

    Args:
        req: ResponsesRequest containing function call outputs
        output_type: Pydantic model class to validate structured content as

    Returns:
        Parsed and validated instance of output_type

    Raises:
        RuntimeError: If no FunctionCallOutputItem found or item has no output
        ValueError: If structured content is missing or result is an error
    """
    if isinstance(req.input, str):
        raise RuntimeError("Cannot extract from string input")

    # Find last FunctionCallOutputItem with output
    for item in reversed(req.input):
        if isinstance(item, FunctionCallOutputItem):
            if not item.output:
                raise RuntimeError(f"FunctionCallOutputItem has no output: {item}")

            # Convert OpenAI output format to MCP CallToolResult
            result = _openai_to_mcp_result(item.output)
            return extract_structured_content(result, output_type)

    raise RuntimeError(f"No FunctionCallOutputItem found in request input for {output_type.__name__}")


def assert_last_call(req: ResponsesRequest, expected_tool: str) -> None:
    """Assert last FunctionCallItem matches expected tool."""
    if isinstance(req.input, str):
        logger.error("Full request dump:")
        logger.error(json.dumps(req.model_dump(mode="json"), indent=2))
        pytest.fail(f"Expected FunctionCallItem for '{expected_tool}', got string input. See log for full request.")

    # Find last FunctionCallItem
    for item in reversed(req.input):
        if isinstance(item, FunctionCallItem):
            actual_name = item.name
            if expected_tool in actual_name:
                return  # Success

            logger.error("Full request dump:")
            logger.error(json.dumps(req.model_dump(mode="json"), indent=2))
            pytest.fail(f"Expected last call to be '{expected_tool}', got '{actual_name}'. See log for full request.")

    logger.error("Full request dump:")
    logger.error(json.dumps(req.model_dump(mode="json"), indent=2))
    pytest.fail(f"No FunctionCallItem found. Expected '{expected_tool}'. See log for full request.")


def extract_output[T: BaseModel](req: ResponsesRequest, output_type: type[T]) -> T:
    """Extract and validate output from last FunctionCallOutputItem."""
    try:
        return get_last_function_output(req, output_type)
    except Exception as e:
        logger.error("Full request dump:")
        logger.error(json.dumps(req.model_dump(mode="json"), indent=2))
        pytest.fail(f"Failed to extract {output_type.__name__}: {e}. See log for full request.")
        raise AssertionError("unreachable")


def assert_and_extract[T: BaseModel](req: ResponsesRequest, expected_tool: str, output_type: type[T]) -> T:
    """Assert expected tool and extract its output."""
    assert_last_call(req, expected_tool)
    return extract_output(req, output_type)


# Type narrowing helpers (TypeGuard)


def is_all_function_calls(
    items: Sequence[SystemMessage | UserMessage | FunctionCallItem],
) -> TypeGuard[Sequence[FunctionCallItem]]:
    """TypeGuard to narrow Sequence[SystemMessage | UserMessage | FunctionCallItem] to Sequence[FunctionCallItem]."""
    return all(isinstance(x, FunctionCallItem) for x in items)


def is_all_user_messages(items: Sequence[UserMessage | FunctionCallItem]) -> TypeGuard[Sequence[UserMessage]]:
    """TypeGuard to narrow Sequence[UserMessage | FunctionCallItem] to Sequence[UserMessage]."""
    return all(isinstance(x, UserMessage) for x in items)
