"""Factories for provider-agnostic LLM clients used across entrypoints.

Goals
- Single-ish construction point for OpenAI-backed clients used by CLIs/services
- Optional HTTP logging via a small wrapper (single JSONL per process/run)
- Return the shared `Model` interface consumed by agents

Environment knobs
- ADGN_OPENAI_HTTP_LOG: if set to a filepath, enable raw HTTP logging there
"""

from __future__ import annotations

import os
from pathlib import Path

from openai import AsyncOpenAI

from openai_utils.http_logging import make_logging_httpx_async_client
from openai_utils.model import Model, ResponsesModel
from openai_utils.types import ReasoningEffort

OPENAI_MAX_RETRIES = 2


def get_async_openai(
    *, log_path: Path | None = None, base_url: str | None = None, api_key: str | None = None
) -> AsyncOpenAI:
    """Return an AsyncOpenAI client, optionally with HTTP logging."""
    http_client = None if log_path is None else make_logging_httpx_async_client(log_path)
    return AsyncOpenAI(http_client=http_client, base_url=base_url, api_key=api_key, max_retries=OPENAI_MAX_RETRIES)


def build_client(
    model: str,
    *,
    base_url: str | None = None,
    log_http_path: Path | None = None,
    reasoning_effort: ReasoningEffort | None = None,
) -> Model:
    """Create a typed Responses client for the given model.

    Respects ADGN_OPENAI_HTTP_LOG if log_http_path is not provided.
    """
    if log_http_path is None:
        env_path = os.environ.get("ADGN_OPENAI_HTTP_LOG")
        log_http_path = Path(env_path) if env_path else None
    inner = get_async_openai(log_path=log_http_path, base_url=base_url)
    return ResponsesModel(client=inner, model=model, reasoning_effort=reasoning_effort)
