"""FastAPI apps for the rspcache proxy and supporting admin surfaces."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import AsyncIterator
from typing import Any, NewType

import canonicaljson
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from openai.types.responses import Response as OpenAIResponse, ResponseCreateParams, ResponseStreamEvent, ResponseUsage
from pydantic import TypeAdapter

from rspcache.models import ErrorPayload, ResponseStatus, response_from_event
from rspcache.responses_db import APIKeyRecord, ResponsesDB

CacheKey = NewType("CacheKey", str)

# Type adapters for OpenAI SDK type aliases (not Pydantic models)
_response_stream_event_adapter: TypeAdapter[ResponseStreamEvent] = TypeAdapter(ResponseStreamEvent)
_response_create_params_adapter: TypeAdapter[ResponseCreateParams] = TypeAdapter(ResponseCreateParams)


class StreamingContext:
    """Shared context for streaming response handling with automatic cleanup."""

    def __init__(self, db: ResponsesDB, key: CacheKey, *, client: httpx.AsyncClient | None = None):
        self.db = db
        self.key = key
        self.response_id: str | None = None
        self._client = client

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is not None:
                if exc_type in (HTTPException, asyncio.CancelledError):
                    return False  # Don't handle these
                await self.db.record_error(
                    self.key,
                    error_reason="Streaming proxy failure",
                    response_id=self.response_id,
                    error=ErrorPayload(message=str(exc_val)),
                )
        finally:
            if self._client:
                await self._client.aclose()
        return False  # Don't suppress exceptions

    async def proxy_stream(self, resp: httpx.Response, start_time: float) -> AsyncIterator[bytes]:
        """Stream response chunks while recording frames to database."""
        text_buffer = ""
        token_usage: ResponseUsage | None = None
        latest_response: OpenAIResponse | None = None
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
                    frame_payload = _response_stream_event_adapter.validate_python(frame)
                    (self.response_id, usage_update, latest) = await _update_response_state(
                        frame=frame_payload, db=self.db, cache_key=self.key, response_id=self.response_id
                    )
                    if usage_update is not None:
                        token_usage = usage_update
                    if latest is not None:
                        latest_response = latest
                    await self.db.append_frame(self.key, frame_payload, response_id=self.response_id)
            trailing_frames = _extract_remaining(text_buffer)
            for frame in trailing_frames:
                frame_payload = _response_stream_event_adapter.validate_python(frame)
                self.response_id, usage_update, latest = await _update_response_state(
                    frame=frame_payload, db=self.db, cache_key=self.key, response_id=self.response_id
                )
                if usage_update is not None:
                    token_usage = usage_update
                if latest is not None:
                    latest_response = latest
                await self.db.append_frame(self.key, frame_payload, response_id=self.response_id)
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            if latest_response is not None and latest_response.usage is not None:
                token_usage = latest_response.usage
            await self.db.finalize_response(
                self.key,
                response_id=self.response_id,
                response_obj=latest_response,
                latency_ms=latency_ms,
                token_usage=token_usage,
            )
        finally:
            await resp.aclose()


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
    header_token = request.headers.get("X-API-Key")
    if isinstance(header_token, str):
        token = header_token.strip()
        if token:
            return token
    return None


def compute_cache_key(body: ResponseCreateParams) -> CacheKey:
    """Compute cache key from OpenAI request body.

    Excludes non-deterministic fields (request_id, request_timestamp, nonce).
    """
    # TypedDict at runtime is a dict; copy and remove non-deterministic fields
    cacheable = dict(body)
    cacheable.pop("request_id", None)
    cacheable.pop("request_timestamp", None)
    cacheable.pop("nonce", None)
    return CacheKey(hashlib.sha256(canonicaljson.encode_canonical_json(cacheable)).hexdigest())


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
    token_usage: ResponseUsage | None = None
    latest_response: OpenAIResponse | None = None

    async def _process_frame(frame_dict: dict[str, Any]) -> None:
        nonlocal response_id, token_usage, latest_response

        frame_payload = _response_stream_event_adapter.validate_python(frame_dict)
        response_id, usage_update, latest = await _update_response_state(
            frame=frame_payload, db=db, cache_key=key, response_id=response_id
        )
        if usage_update is not None:
            token_usage = usage_update
        if latest is not None:
            latest_response = latest
        await db.append_frame(key, frame_payload, response_id=response_id)

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
        validated_body = _response_create_params_adapter.validate_python(body)
        key = compute_cache_key(validated_body)
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
            async with StreamingContext(db=db, key=key, client=client) as ctx:
                async for chunk in ctx.proxy_stream(resp, start_time=start_time):
                    yield chunk

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

    response_model = OpenAIResponse.model_validate(resp_json)
    response_id = response_model.id
    await db.mark_in_progress(key, response_id)
    latency_ms = int((time.perf_counter() - start_time) * 1000)
    token_usage = response_model.usage

    await db.finalize_response(
        key, response_id=response_id, response_obj=response_model, latency_ms=latency_ms, token_usage=token_usage
    )

    headers = {"X-Cache-Hit": "0", "X-Cache-Key": key}
    return JSONResponse(content=resp_json, status_code=resp.status_code, headers=headers)


async def _update_response_state(
    *, frame: ResponseStreamEvent, db: ResponsesDB, cache_key: str, response_id: str | None
) -> tuple[str | None, ResponseUsage | None, OpenAIResponse | None]:
    """Update cached response metadata from a stream frame and record progress."""

    response_obj = response_from_event(frame)
    if response_obj is None:
        return response_id, None, None

    updated_id = response_id
    if response_obj.id and response_obj.id != response_id:
        updated_id = response_obj.id
        await db.mark_in_progress(cache_key, updated_id)

    return updated_id, response_obj.usage, response_obj
