"""OpenAI client wrappers with verbatim HTTP logging (masked auth).

This module provides small helpers to construct OpenAI/AsyncOpenAI clients
that log raw HTTP requests/responses to a JSONL file for diagnostics.

Notes
- Authorization header is masked (***).
- Bodies are logged as UTF-8 text with errors="ignore" to avoid crashes on
  binary content.
- Log format: one JSON object per line with keys {kind, ...} where kind is
  "request" or "response".

Typical usage

from pathlib import Path
from openai_utils.http_logging import make_logged_async_openai

client = make_logged_async_openai(Path("./openai_http.jsonl"))
# pass `client` where an AsyncOpenAI is expected

"""

from __future__ import annotations

import json
import logging
from functools import partial
from pathlib import Path
from typing import Any

import httpx
from openai import AsyncOpenAI

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
    try:
        body = req.content or b""
    except Exception as e:
        logger.debug("failed to read request body for logging: %s", e)
        body = b""
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
    try:
        req_body = req.content or b""
    except Exception as e:
        logger.debug("failed to read request body for logging: %s", e)
        req_body = b""
    # Safely read response body for streaming responses
    resp_body_bytes: bytes = b""
    try:
        resp_body_bytes = await resp.aread()
    except Exception as e:
        logger.debug("failed to aread response body: %s", e)
        try:
            resp_body_bytes = resp.content
        except Exception as e2:
            logger.debug("failed to access response.content: %s", e2)
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
    try:
        body = req.content or b""
    except Exception as e:
        logger.debug("failed to read request body for logging: %s", e)
        body = b""

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
    try:
        req_body = req.content or b""
    except Exception as e:
        logger.debug("failed to read request body for logging: %s", e)
        req_body = b""

    # Safely read response body for streaming responses
    resp_body_bytes: bytes = b""
    try:
        resp_body_bytes = await resp.aread()
    except Exception as e:
        logger.debug("failed to aread response body: %s", e)
        try:
            resp_body_bytes = resp.content
        except Exception as e2:
            logger.debug("failed to access response.content: %s", e2)
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


def make_logged_async_openai(log_path: Path | str) -> AsyncOpenAI:
    """Create an AsyncOpenAI client that logs raw HTTP traffic to log_path.

    The returned client owns an httpx.AsyncClient with event hooks installed.
    The caller is responsible for closing the OpenAI client when done.
    """
    p = Path(log_path)
    http = httpx.AsyncClient(
        event_hooks={"request": [partial(_on_request, path=p)], "response": [partial(_on_response, path=p)]}
    )
    return AsyncOpenAI(http_client=http)


def make_logger_logged_async_openai(logger_name: str = "openai.http") -> AsyncOpenAI:
    """Create an AsyncOpenAI client that logs raw HTTP traffic to a Python logger at DEBUG level.

    The returned client owns an httpx.AsyncClient with event hooks installed.
    The caller is responsible for closing the OpenAI client when done.
    """
    target_logger = logging.getLogger(logger_name)
    http = httpx.AsyncClient(
        event_hooks={
            "request": [partial(_log_request_to_logger, logger=target_logger)],
            "response": [partial(_log_response_to_logger, logger=target_logger)],
        }
    )
    return AsyncOpenAI(http_client=http)
