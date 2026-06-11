"""Shared wire-API-shape primitives for logical models."""

from __future__ import annotations

from enum import StrEnum


class LLMApiShape(StrEnum):
    """Wire API shape used for a logical model.

    `responses` and `chat_completions` are OpenAI-compatible; `anthropic` is the
    Anthropic Messages shape (e.g. Claude, or z.ai's GLM Anthropic endpoint).
    """

    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"
    ANTHROPIC = "anthropic"
