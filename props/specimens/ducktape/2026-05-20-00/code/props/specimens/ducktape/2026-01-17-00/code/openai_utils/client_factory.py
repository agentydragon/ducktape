"""Factories for provider-agnostic LLM clients used across entrypoints.

Goals
- Single-ish construction point for OpenAI-backed clients used by CLIs/services
- Optional HTTP logging via a small wrapper (single JSONL per process/run)
- Return the provider-agnostic interface `ResponsesClient` consumed by agents

Environment knobs
- ADGN_OPENAI_HTTP_LOG: if set to a filepath, enable raw HTTP logging there
"""

from __future__ import annotations

import os
from pathlib import Path

from openai import AsyncOpenAI

from openai_utils.http_logging import make_logged_async_openai, make_logger_logged_async_openai
from openai_utils.model import BoundOpenAIModel, OpenAIModelProto
from openai_utils.retry import RetryingOpenAIModel
from openai_utils.types import ReasoningEffort


def _get_async_openai(*, log_path: Path | str | None = None) -> AsyncOpenAI:
    """Return a cached AsyncOpenAI client (optionally with HTTP logging).

    Cache key is the logging path (None vs specific path). This ensures we avoid
    constructing many clients per process while still allowing an opt-in logging
    variant when explicitly requested.
    """
    return make_logged_async_openai(Path(log_path)) if log_path else AsyncOpenAI()


def get_async_openai(log_http_path: Path | str | None = None) -> AsyncOpenAI:
    """Public helper to obtain a shared AsyncOpenAI client (optionally with HTTP logging)."""
    return _get_async_openai(log_path=log_http_path)


def build_client(
    model: str,
    *,
    log_http_path: Path | str | None = None,
    enable_debug_logging: bool = False,
    reasoning_effort: ReasoningEffort | None = None,
) -> OpenAIModelProto:
    """Create a typed, retrying Responses client for the given model.

    - Respects ADGN_OPENAI_HTTP_LOG if log_http_path is not provided
    - If enable_debug_logging=True, logs HTTP traffic to Python logger at DEBUG level
    """
    if enable_debug_logging:
        inner = make_logger_logged_async_openai()
    elif log_http_path is None:
        env_path = os.environ.get("ADGN_OPENAI_HTTP_LOG")
        inner = _get_async_openai(log_path=Path(env_path)) if env_path else _get_async_openai()
    else:
        inner = _get_async_openai(log_path=log_http_path)
    base = BoundOpenAIModel(client=inner, model=model, reasoning_effort=reasoning_effort)
    return RetryingOpenAIModel(base=base)
