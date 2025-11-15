from __future__ import annotations

from adgn.openai_utils.model import AssistantMessageOut, ResponsesResult


def first_assistant_text(response: ResponsesResult) -> str:
    """Raises ValueError if no assistant text found."""
    for item in response.output:
        if isinstance(item, AssistantMessageOut):
            text = item.text
            if text:
                return text
    raise ValueError("No assistant message with text found in response")


def try_first_assistant_text(response: ResponsesResult) -> str | None:
    for item in response.output:
        if isinstance(item, AssistantMessageOut):
            text = item.text
            if text:
                return text
    return None


def all_assistant_text(response: ResponsesResult) -> list[str]:
    texts = []
    for item in response.output:
        if isinstance(item, AssistantMessageOut):
            text = item.text
            if text:
                texts.append(text)
    return texts


def concatenate_assistant_text(response: ResponsesResult, separator: str = "\n\n") -> str:
    return separator.join(all_assistant_text(response))
