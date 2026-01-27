"""OpenAI client wrappers with verbatim HTTP logging (masked auth).

This module provides helpers to construct httpx clients and OpenAI clients
that log raw HTTP requests/responses for diagnostics.

Notes
- Authorization header is masked (***).
- Bodies are logged as UTF-8 text with errors="ignore" to avoid crashes on
  binary content.
- Log format: one JSON object per line with keys {kind, ...} where kind is
  "request" or "response".

For typical usage, prefer ``openai_utils.client_factory.build_client()`` which
handles logging configuration automatically.
"""

from __future__ import annotations

import json
import logging
from functools import partial
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def _log_write(path: Path, record: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        # Best-effort logging; never crash the caller, but surface failure for diagnostics
        logger.debug("failed to write HTTP log: %s", e)


async def _on_request(req: httpx.Request, *, path: Path) -> None:  # pragma: no cover - HTTP hook
    headers = {k: ("***" if k.lower() == "authorization" else v) for k, v in req.headers.items()}
    body = req.content or b""
    await _log_write(
        path,
        {
            "kind": "request",
            "method": req.method,
            "url": str(req.url),
            "headers": headers,
            "body": body.decode("utf-8", errors="ignore"),
        },
    )


async def _on_response(resp: httpx.Response, *, path: Path) -> None:  # pragma: no cover - HTTP hook
    req = resp.request
    headers = {k: ("***" if k.lower() == "authorization" else v) for k, v in req.headers.items()}
    req_body = req.content or b""
    # Read response body; may fail if stream already consumed
    try:
        resp_body_bytes = await resp.aread()
    except (httpx.StreamConsumed, httpx.StreamClosed, httpx.ReadError) as e:
        logger.debug("failed to read response body: %s", e)
        resp_body_bytes = b""
    await _log_write(
        path,
        {
            "kind": "response",
            "status": resp.status_code,
            "req_url": str(req.url),
            "req_headers": headers,
            "req_body": req_body.decode("utf-8", errors="ignore"),
            "resp_headers": dict(resp.headers),
            "resp_body": resp_body_bytes.decode("utf-8", errors="ignore"),
        },
    )


async def _log_request_to_logger(req: httpx.Request, *, logger: logging.Logger) -> None:  # pragma: no cover - HTTP hook
    headers = {k: ("***" if k.lower() == "authorization" else v) for k, v in req.headers.items()}
    body = req.content or b""
    logger.debug(
        "OpenAI Request: %s %s\nHeaders: %s\nBody: %s",
        req.method,
        str(req.url),
        json.dumps(headers, ensure_ascii=False),
        body.decode("utf-8", errors="ignore"),
    )


async def _log_response_to_logger(
    resp: httpx.Response, *, logger: logging.Logger
) -> None:  # pragma: no cover - HTTP hook
    req = resp.request
    headers = {k: ("***" if k.lower() == "authorization" else v) for k, v in req.headers.items()}
    req_body = req.content or b""
    # Read response body; may fail if stream already consumed
    try:
        resp_body_bytes = await resp.aread()
    except (httpx.StreamConsumed, httpx.StreamClosed, httpx.ReadError) as e:
        logger.debug("failed to read response body: %s", e)
        resp_body_bytes = b""
    logger.debug(
        "OpenAI Response: %d %s\nRequest Headers: %s\nRequest Body: %s\nResponse Headers: %s\nResponse Body: %s",
        resp.status_code,
        str(req.url),
        json.dumps(headers, ensure_ascii=False),
        req_body.decode("utf-8", errors="ignore"),
        json.dumps(dict(resp.headers), ensure_ascii=False),
        resp_body_bytes.decode("utf-8", errors="ignore"),
    )


def make_logging_httpx_async_client(log_path: Path) -> httpx.AsyncClient:
    """Return an ``httpx.AsyncClient`` with request/response logging.

    The client is *ready* to be passed to :class:`AsyncOpenAI` as its
    ``http_client`` argument.  No :class:`AsyncOpenAI` instance is created
    here - that keeps the functionality focused on HTTP log handling.
    """
    return httpx.AsyncClient(
        event_hooks={
            "request": [partial(_on_request, path=log_path)],
            "response": [partial(_on_response, path=log_path)],
        }
    )


def make_logger_logging_httpx_async_client(logger_name: str = "openai.http") -> httpx.AsyncClient:
    """Return an httpx.AsyncClient that logs raw HTTP traffic to a Python logger at DEBUG level."""
    target_logger = logging.getLogger(logger_name)
    return httpx.AsyncClient(
        event_hooks={
            "request": [partial(_log_request_to_logger, logger=target_logger)],
            "response": [partial(_log_response_to_logger, logger=target_logger)],
        }
    )
