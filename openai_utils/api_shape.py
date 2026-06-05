"""Shared OpenAI-compatible API shape primitives."""

from __future__ import annotations

from enum import StrEnum


class LLMApiShape(StrEnum):
    """OpenAI-compatible API shape used for a logical model."""

    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"
