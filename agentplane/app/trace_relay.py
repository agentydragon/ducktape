"""Authenticated, bounded same-origin relay for browser OTLP/HTTP trace exports."""

from __future__ import annotations

import os
from urllib.parse import unquote

import httpx
from fastapi import HTTPException, Request, status
from fastapi.responses import Response

MAX_TRACE_PAYLOAD_BYTES = 1_048_576


def traces_endpoint() -> str | None:
    """Resolve only server-side OTLP configuration; clients cannot choose a destination."""
    protocol = (
        os.environ.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL")
        or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL")
        or "http/protobuf"
    ).lower()
    if protocol != "http/protobuf":
        return None
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if endpoint:
        return endpoint
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return None
    return f"{endpoint.rstrip('/')}/v1/traces"


def _configured_headers() -> dict[str, str]:
    encoded = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS")
    if encoded is None:
        encoded = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
    headers: dict[str, str] = {}
    for pair in encoded.split(","):
        if not pair:
            continue
        name, separator, value = pair.partition("=")
        if separator and name.strip():
            headers[name.strip()] = unquote(value.strip())
    return headers


async def _bounded_body(request: Request) -> bytes:
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > MAX_TRACE_PAYLOAD_BYTES:
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "trace payload is too large")
        except ValueError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid content length") from error

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_TRACE_PAYLOAD_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "trace payload is too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def relay_traces(request: Request) -> Response:
    """Forward protobuf bytes without decoding, persisting, or logging trace payloads."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-protobuf":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "expected application/x-protobuf")
    if request.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "compressed trace payloads are not accepted")

    endpoint = traces_endpoint()
    if endpoint is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "trace receiver is not configured")
    payload = await _bounded_body(request)
    headers = {"content-type": "application/x-protobuf", **_configured_headers()}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5, connect=2)) as client:
            upstream = await client.post(endpoint, content=payload, headers=headers)
    except httpx.HTTPError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "trace receiver unavailable") from error
    if not 200 <= upstream.status_code < 300:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "trace receiver rejected the export")
    return Response(status_code=status.HTTP_200_OK)
