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

from openai_utils.http_logging import make_logger_logging_httpx_async_client, make_logging_httpx_async_client
from openai_utils.model import BoundOpenAIModel, OpenAIModelProto
from openai_utils.retry import RetryingOpenAIModel
from openai_utils.types import ReasoningEffort


def get_async_openai(*, log_path: Path | None = None, base_url: str | None = None) -> AsyncOpenAI:
    """Return an AsyncOpenAI client, optionally with HTTP logging."""
    if log_path is None:
        return AsyncOpenAI(base_url=base_url)
    http_client = make_logging_httpx_async_client(log_path)
    return AsyncOpenAI(http_client=http_client, base_url=base_url)


def build_client(
    model: str,
    *,
    base_url: str | None = None,
    log_http_path: Path | None = None,
    enable_debug_logging: bool = False,
    reasoning_effort: ReasoningEffort | None = None,
) -> OpenAIModelProto:
    """Create a typed, retrying Responses client for the given model.

    - Respects ADGN_OPENAI_HTTP_LOG if log_http_path is not provided
    - If enable_debug_logging=True, logs HTTP traffic to Python logger at DEBUG level
    """
    if enable_debug_logging:
        inner = AsyncOpenAI(http_client=make_logger_logging_httpx_async_client())
    elif log_http_path is None:
        env_path = os.environ.get("ADGN_OPENAI_HTTP_LOG")
        inner = (
            get_async_openai(log_path=Path(env_path), base_url=base_url)
            if env_path
            else get_async_openai(base_url=base_url)
        )
    else:
        inner = get_async_openai(log_path=log_http_path, base_url=base_url)
    base = BoundOpenAIModel(client=inner, model=model, reasoning_effort=reasoning_effort)
    return RetryingOpenAIModel(base=base)
