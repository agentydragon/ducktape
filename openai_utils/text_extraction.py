from __future__ import annotations

from openai_utils.model import AssistantMessageOut, ResponsesResult


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
