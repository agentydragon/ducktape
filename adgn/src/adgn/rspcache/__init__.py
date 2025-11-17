"""FastAPI apps for the rspcache proxy and supporting admin surfaces."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import hashlib
import json
import os
import time
from typing import Any

import canonicaljson
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
import httpx
from openai.types.responses import (
    Response as OpenAIResponse,
    ResponseCreateParams,
    ResponseStreamEvent,
    ResponseUsage,
)

from adgn.rspcache.models import (
    ErrorPayload,
    ResponseStatus,
    parse_response,
    stream_event_final_response,
    stream_event_response_id,
    stream_event_usage,
)
from adgn.rspcache.responses_db import APIKeyRecord, ResponsesDB


@asynccontextmanager
async def record_errors_to_db(
    db: ResponsesDB, key: str, response_id: str | None = None, error_reason: str | None = None
):
    """Context manager to automatically record exceptions to database.

    Args:
        db: Database instance
        key: Cache key
        response_id: Optional response ID
        error_reason: Default error reason if not provided by exception
    """
    try:
        yield
    except HTTPException:
        # Don't record HTTPExceptions from FastAPI - they're user-facing errors
        raise
    except asyncio.CancelledError:
        # Don't record cancellations
        raise
    except Exception as exc:
        reason = error_reason or str(exc)
        await db.record_error(key, error_reason=reason, response_id=response_id, error=ErrorPayload(message=reason))
        raise


HTTP_ERROR_MIN = 400
SSE_PREFIX = "data:"

_db = ResponsesDB()


def get_db() -> ResponsesDB:
    return _db


def _require_api_keys() -> bool:
    value = os.environ.get("RSPCACHE_REQUIRE_API_KEY", "0")
    return value.lower() in {"1", "true", "yes"}


def _load_openai_keys() -> dict[str, str]:
    mapping: dict[str, str] = {}
    if default := os.environ.get("OPENAI_API_KEY"):
        mapping.setdefault("default", default)
    if mapping_env := os.environ.get("ADGN_OPENAI_KEYS"):
        for raw in mapping_env.split(","):
            if not (item := raw.strip()) or "=" not in item:
                continue
            alias, key = item.split("=", 1)
            mapping[alias.strip()] = key.strip()
    prefix = "ADGN_OPENAI_KEY_"
    for env_key, env_val in os.environ.items():
        if env_key.startswith(prefix):
            alias = env_key[len(prefix) :].lower()
            mapping[alias] = env_val
    return mapping


def _resolve_openai_api_key(alias: str | None) -> str:
    mapping = _load_openai_keys()
    if not (api_key := mapping.get(alias or "default")):
        raise HTTPException(status_code=500, detail=f"OPENAI API key not configured for alias {alias}")
    return api_key


def _extract_client_token(request: Request, authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key and (token := x_api_key.strip()):
        return token
    if authorization:
        for prefix in ("Bearer ", "bearer "):
            if (stripped := authorization.removeprefix(prefix)) != authorization and (token := stripped.strip()):
                return token
    if (header_token := request.headers.get("X-API-Key")) and (token := header_token.strip()):
        return token
    return None


def make_key_from_body(body: ResponseCreateParams) -> str:
    """Create cache key from OpenAI request body.

    Excludes non-deterministic fields via model_copy for validation.
    """
    # Use model_copy to exclude non-deterministic fields, benefiting from Pydantic validation
    cacheable = body.model_copy(update={"request_id": None, "request_timestamp": None, "nonce": None})
    keyed = cacheable.model_dump(mode="json", exclude_none=True, exclude_unset=True)
    return hashlib.sha256(canonicaljson.encode_canonical_json(keyed)).hexdigest()


def _extract_frames(buffer: str) -> tuple[str, list[dict[str, Any]]]:
    if "\n" not in buffer:
        return buffer, []
    parts = buffer.split("\n")
    remainder = parts[-1]
    frames: list[dict[str, Any]] = []
    for part in parts[:-1]:
        line = part.strip()
        if not line:
            continue
        content = line.removeprefix(SSE_PREFIX).lstrip()
        if content == "[DONE]":
            continue
        try:
            frames.append(json.loads(content))
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=502, detail=f"Non-JSON NDJSON frame: {content[:200]!r}") from exc
    return remainder, frames


def _extract_remaining(buffer: str) -> list[dict[str, Any]]:
    buffer = buffer.strip()
    if not buffer:
        return []
    buffer = buffer.removeprefix(SSE_PREFIX).lstrip()
    try:
        return [json.loads(buffer)]
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail=f"Non-JSON trailing NDJSON partial: {buffer[:200]!r}") from exc


proxy_app = FastAPI(title="adgn-llm OpenAI Responses proxy")
APP = proxy_app


@proxy_app.on_event("startup")
async def _startup() -> None:
    await _db.init()


@proxy_app.on_event("shutdown")
async def _shutdown() -> None:
    await _db.close()


@proxy_app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def _proxy_stream(
    resp: httpx.Response, *, db: ResponsesDB, key: str, response_id: str | None, start_time: float
) -> AsyncIterator[bytes]:
    text_buffer = ""
    ordinal = 0
    token_usage: ResponseUsage | None = None
    latest_response: OpenAIResponse | None = None

    async def _process_frame(frame_dict: dict[str, Any]) -> None:
        nonlocal ordinal, response_id, token_usage, latest_response

        frame_payload = ResponseStreamEvent.model_validate(frame_dict)
        ordinal += 1
        if (maybe_response_id := stream_event_response_id(frame_payload)) and response_id != maybe_response_id:
            response_id = maybe_response_id
            await db.mark_in_progress(key, response_id)
        if usage := stream_event_usage(frame_payload):
            token_usage = usage
        if (response_candidate := stream_event_final_response(frame_payload)) is not None:
            latest_response = response_candidate
        await db.append_frame(key, frame_payload, ordinal=ordinal, response_id=response_id)

    try:
        async for chunk in resp.aiter_bytes():
            if not chunk:
                continue
            yield chunk
            try:
                decoded = chunk.decode("utf-8")
            except UnicodeDecodeError:
                continue
            text_buffer += decoded
            text_buffer, parsed = _extract_frames(text_buffer)
            if not parsed:
                continue
            for frame in parsed:
                await _process_frame(frame)

        trailing_frames = _extract_remaining(text_buffer)
        for frame in trailing_frames:
            await _process_frame(frame)

        latency_ms = int((time.perf_counter() - start_time) * 1000)
        if latest_response is not None and latest_response.usage is not None:
            token_usage = latest_response.usage
        await db.finalize_response(
            key, response_id=response_id, response_obj=latest_response, latency_ms=latency_ms, token_usage=token_usage
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        error_reason = "Streaming proxy failure"
        await db.record_error(
            key, error_reason=error_reason, response_id=response_id, error=ErrorPayload(message=error_reason)
        )
        raise
    finally:
        await resp.aclose()


def _relay_error_response(resp: httpx.Response) -> Response:
    media_type = resp.headers.get("content-type")
    body = resp.content
    return Response(content=body, status_code=resp.status_code, media_type=media_type)


@proxy_app.post("/v1/responses")
async def responses_endpoint(
    request: Request,
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None, convert_underscores=False),
    db: ResponsesDB = Depends(get_db),  # noqa: B008
) -> Response:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}") from exc
    model = body.get("model")
    if not isinstance(model, str):
        raise HTTPException(status_code=400, detail="Request body must include model")

    token = _extract_client_token(request, authorization, x_api_key)
    api_keys_required = _require_api_keys()
    api_key_record: APIKeyRecord | None = None
    if token:
        api_key_record = await db.verify_api_key(token)
        if api_key_record is None:
            raise HTTPException(status_code=401, detail="Invalid API key")
    elif api_keys_required:
        raise HTTPException(status_code=401, detail="API key required")

    upstream_alias = api_key_record.upstream_alias if api_key_record else "default"
    upstream_key = _resolve_openai_api_key(upstream_alias)

    cache_skip = body.get("cache_skip") in (True, "true", "True", 1)

    # Validate request body structure and generate cache key
    try:
        validated_body = ResponseCreateParams.model_validate(body)
        key = make_key_from_body(validated_body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid request: {exc}") from exc

    is_stream = bool(body.get("stream"))
    if not cache_skip:
        cached = await db.get_response(key)
        if cached and cached.status == ResponseStatus.COMPLETE:
            cached_payload = await db.get_cached_response_payload(key)
            if cached_payload is not None:
                headers = {"X-Cache-Hit": "1", "X-Cache-Key": key}
                return JSONResponse(content=cached_payload, status_code=200, headers=headers)

    await db.claim_key(key, model, body, api_key_record)
    await db.mark_in_progress(key, None)

    upstream_url = os.environ.get("OPENAI_API_BASE", "https://api.openai.com").rstrip("/") + "/v1/responses"
    headers = {"Authorization": f"Bearer {upstream_key}", "Content-Type": "application/json"}

    start_time = time.perf_counter()

    if is_stream:
        client = httpx.AsyncClient(timeout=None)
        request_obj = client.build_request("POST", upstream_url, json=body, headers=headers)
        try:
            resp = await client.send(request_obj, stream=True)
        except Exception as exc:
            await client.aclose()
            await db.record_error(key, error_reason=str(exc), response_id=None, error=ErrorPayload(message=str(exc)))
            raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc}") from exc
        if resp.status_code >= HTTP_ERROR_MIN:
            payload = await resp.aread()
            await db.record_error(
                key,
                error_reason=f"Upstream status {resp.status_code}",
                response_id=None,
                error=ErrorPayload(
                    message="Upstream error",
                    detail={"status": resp.status_code, "body": payload.decode(errors="ignore")},
                ),
            )
            await resp.aclose()
            await client.aclose()
            upstream_error = httpx.Response(status_code=resp.status_code, content=payload, headers=resp.headers)
            return _relay_error_response(upstream_error)

        async def event_stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in _proxy_stream(resp, db=db, key=key, response_id=None, start_time=start_time):
                    yield chunk
            finally:
                await client.aclose()

        return StreamingResponse(
            event_stream(), media_type="text/event-stream", headers={"X-Cache-Hit": "0", "X-Cache-Key": key}
        )

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.post(upstream_url, json=body, headers=headers)
        except Exception as exc:
            await db.record_error(key, error_reason=str(exc), response_id=None, error=ErrorPayload(message=str(exc)))
            raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc}") from exc

    if resp.status_code >= HTTP_ERROR_MIN:
        detail = resp.text
        await db.record_error(
            key,
            error_reason=f"Upstream status {resp.status_code}",
            response_id=None,
            error=ErrorPayload(message="Upstream error", detail={"status": resp.status_code, "body": detail}),
        )
        return _relay_error_response(resp)

    try:
        resp_json = resp.json()
    except Exception as exc:
        await db.record_error(
            key,
            error_reason="Upstream returned non-JSON response",
            response_id=None,
            error=ErrorPayload(message="Upstream returned non-JSON response", detail={"body": resp.text}),
        )
        raise HTTPException(status_code=502, detail="Upstream returned non-JSON response") from exc

    response_model = parse_response(resp_json)
    response_id = response_model.id
    await db.mark_in_progress(key, response_id)
    latency_ms = int((time.perf_counter() - start_time) * 1000)
    token_usage = response_model.usage

    await db.finalize_response(
        key, response_id=response_id, response_obj=response_model, latency_ms=latency_ms, token_usage=token_usage
    )

    headers = {"X-Cache-Hit": "0", "X-Cache-Key": key}
    return JSONResponse(content=resp_json, status_code=resp.status_code, headers=headers)
