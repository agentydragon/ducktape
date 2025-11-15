"""High-level text extraction utilities for OpenAI Responses API.

Provides convenient functions for extracting assistant text from ResponsesResult objects.
For lower-level type-safe conversions, see adgn.llm.sysrw.openai_typing.
For lenient wire format handling, see adgn.llm.sysrw.extract_common.
"""

from __future__ import annotations

from adgn.openai_utils.model import AssistantMessageOut, ResponsesResult


def first_assistant_text(response: ResponsesResult) -> str:
    """Extract first assistant message text from response.output.

    Args:
        response: ResponsesResult from API call

    Returns:
        First assistant text found

    Raises:
        ValueError: If no assistant message with text found in response
    """
    for item in response.output:
        if isinstance(item, AssistantMessageOut):
            text = item.text
            if text:
                return text

    raise ValueError("No assistant message with text found in response")


def try_first_assistant_text(response: ResponsesResult) -> str | None:
    """Extract first assistant message text, returning None if not found.

    Args:
        response: ResponsesResult from API call

    Returns:
        First assistant text, or None if no assistant message with text found
    """
    for item in response.output:
        if isinstance(item, AssistantMessageOut):
            text = item.text
            if text:
                return text
    return None


def all_assistant_text(response: ResponsesResult) -> list[str]:
    """Extract all assistant message texts from response.output.

    Args:
        response: ResponsesResult from API call

    Returns:
        List of all assistant texts (may be empty)
    """
    texts = []
    for item in response.output:
        if isinstance(item, AssistantMessageOut):
            text = item.text
            if text:
                texts.append(text)
    return texts


def concatenate_assistant_text(response: ResponsesResult, separator: str = "\n\n") -> str:
    """Extract and concatenate all assistant texts with separator.

    Args:
        response: ResponsesResult from API call
        separator: String to join multiple texts (default: double newline)

    Returns:
        Concatenated assistant text (empty string if none found)
    """
    return separator.join(all_assistant_text(response))
