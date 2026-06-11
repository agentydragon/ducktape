from __future__ import annotations

from collections.abc import Sequence

from openai_utils.model import AssistantMessageOut, InputItem, ResponsesResult


def all_assistant_text(response: ResponsesResult) -> list[str]:
    texts = []
    for item in response.output:
        if isinstance(item, AssistantMessageOut):
            text = item.text
            if text:
                texts.append(text)
    return texts


def first_assistant_text(response: ResponsesResult) -> str:
    """Raises ValueError if no assistant text found."""
    texts = all_assistant_text(response)
    if not texts:
        raise ValueError("No assistant message with text found in response")
    return texts[0]


def extract_input_text_content(messages: Sequence[InputItem | AssistantMessageOut]) -> list[str]:
    """Extract input_text content from messages, returning list of text strings.

    Handles both InputItem (which can have content lists) and AssistantMessageOut.
    """
    texts: list[str] = []
    for item in messages:
        # Convert to dict for uniform content extraction
        msgd = item.model_dump()
        # Extract input_text content
        contents = msgd.get("content") or []
        for c in contents:
            if isinstance(c, dict) and c.get("type") == "input_text":
                text = c.get("text")
                if isinstance(text, str):
                    texts.append(text)
    return texts
