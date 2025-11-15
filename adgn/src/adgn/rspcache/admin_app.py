from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from asyncpg import UniqueViolationError
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pydantic.config import ConfigDict

from adgn.rspcache.models import (
    FRAME_ADAPTER,
    stream_event_event_id,
)
from adgn.rspcache.responses_db import (
    APIKeyRecord,
    ClientAPIKey,
    Response,
    ResponseFrame,
    ResponsesDB,
)

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
DEFAULT_FRAME_LIMIT = 200

_db = ResponsesDB()


def get_db() -> ResponsesDB:
    return _db


def _load_upstream_keys() -> list[str]:
    aliases: set[str] = set()
    default = os.environ.get("OPENAI_API_KEY")
    if default:
        aliases.add("default")
    mapping_env = os.environ.get("ADGN_OPENAI_KEYS")
    if mapping_env:
        for item in mapping_env.split(","):
            if not item.strip() or "=" not in item:
                continue
            alias, _ = item.split("=", 1)
            aliases.add(alias.strip())
    prefix = "ADGN_OPENAI_KEY_"
    for env_key in os.environ:
        if env_key.startswith(prefix):
            alias = env_key[len(prefix) :].lower()
            aliases.add(alias)
    return sorted(aliases or {"default"})


class CreateKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    alias: str = Field(default="default", min_length=1, max_length=128)


class FrameRecordModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ordinal: int
    frame_type: str | None = None
    event_id: str | None = None
    created_ts: datetime
    frame: Any


class ResponseRecordModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    response_id: str | None = None
    api_key_id: UUID | None = None
    api_key_name: str | None = None
    model: str
    status: str
    status_reason: str | None = None
    created_ts: datetime
    last_update_ts: datetime
    latency_ms: int | None = None
    token_usage: dict[str, Any] | None = None
    request_body: dict[str, Any] | None = None
    final_response: dict[str, Any] | None = None
    response_error: dict[str, Any] | None = None


class ResponseListModel(BaseModel):
    items: list[ResponseRecordModel]
    limit: int
    offset: int
    total: int


class FrameListModel(BaseModel):
    items: list[FrameRecordModel]
    count: int
    limit: int | None
    after: int | None


class APIKeyModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    token_prefix: str
    upstream_alias: str
    created_ts: datetime
    revoked_ts: datetime | None = None


class APIKeyListModel(BaseModel):
    items: list[APIKeyModel]
    count: int


class CreateKeyResponse(BaseModel):
    token: str
    record: APIKeyModel


class RevokeKeyResponse(BaseModel):
    ok: bool


class UpstreamKeyListModel(BaseModel):
    items: list[str]


admin_app = FastAPI(title="rspcache admin")
ADMIN_APP = admin_app


@admin_app.on_event("startup")
async def _startup() -> None:
    await _db.init()
    await _db.ensure_event_listener()


@admin_app.on_event("shutdown")
async def _shutdown() -> None:
    await _db.stop_event_listener()
    await _db.close()


def _to_response_model(
    record: Response,
    *,
    response_payload: dict[str, Any] | None = None,
    error_payload: dict[str, Any] | None = None,
    token_usage: dict[str, Any] | None = None,
) -> ResponseRecordModel:
    api_key_name = record.api_key.name if record.api_key else None
    snapshot = record.snapshot.to_model() if record.snapshot else None
    final_response_json = response_payload
    error_json = error_payload
    token_usage_json = token_usage
    if snapshot is not None:
        if final_response_json is None and snapshot.response is not None:
            final_response_json = snapshot.response.model_dump(mode="json")
        if error_json is None and snapshot.error is not None:
            error_json = snapshot.error.model_dump(mode="json")
        if token_usage_json is None and snapshot.token_usage is not None:
            token_usage_json = snapshot.token_usage.model_dump(mode="json")
    return ResponseRecordModel(
        key=record.key,
        response_id=record.response_id,
        api_key_id=record.api_key_id,
        api_key_name=api_key_name,
        model=record.model,
        status=record.status,
        status_reason=record.status_reason,
        created_ts=record.created_ts,
        last_update_ts=record.last_update_ts,
        latency_ms=record.latency_ms,
        token_usage=token_usage_json or record.token_usage_json,
        request_body=record.request_body_json,
        final_response=final_response_json,
        response_error=error_json,
    )


def _to_frame_model(frame: ResponseFrame) -> FrameRecordModel:
    payload = FRAME_ADAPTER.validate_python(frame.frame_json)
    return FrameRecordModel(
        ordinal=frame.ordinal,
        frame_type=frame.frame_type or payload.type,
        event_id=frame.event_id or stream_event_event_id(payload),
        created_ts=frame.created_ts,
        frame=payload.model_dump(mode="json"),
    )


def _to_api_key_model(record: ClientAPIKey | APIKeyRecord) -> APIKeyModel:
    return APIKeyModel(
        id=getattr(record, "id"),
        name=record.name,
        token_prefix=record.token_prefix,
        upstream_alias=record.upstream_alias,
        created_ts=record.created_ts,
        revoked_ts=record.revoked_ts,
    )


@admin_app.get("/api/responses", response_model=ResponseListModel)
async def list_responses(
    limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(0, ge=0),
    db: ResponsesDB = Depends(get_db),
) -> ResponseListModel:
    items, total = await db.list_responses(limit=limit, offset=offset)
    return ResponseListModel(
        items=[_to_response_model(item) for item in items],
        limit=limit,
        offset=offset,
        total=total,
    )


@admin_app.get("/api/responses/{identifier}", response_model=ResponseRecordModel)
async def get_response(identifier: str, db: ResponsesDB = Depends(get_db)) -> ResponseRecordModel:
    detail = await db.get_response_detail(identifier)
    if detail is None:
        raise HTTPException(status_code=404, detail="Response not found")
    snapshot = detail.snapshot
    return _to_response_model(
        detail.record,
        response_payload=snapshot.response.model_dump(mode="json")
        if snapshot and snapshot.response
        else None,
        error_payload=snapshot.error.model_dump(mode="json") if snapshot and snapshot.error else None,
        token_usage=snapshot.token_usage.model_dump(mode="json") if snapshot and snapshot.token_usage else None,
    )


@admin_app.get("/api/responses/{identifier}/frames", response_model=FrameListModel)
async def get_frames(
    identifier: str,
    limit: int | None = Query(DEFAULT_FRAME_LIMIT, ge=1, le=1000),
    after: int | None = Query(None, ge=0),
    db: ResponsesDB = Depends(get_db),
) -> FrameListModel:
    frames = await db.get_frames(identifier, limit=limit, after_ordinal=after)
    return FrameListModel(
        items=[_to_frame_model(frame) for frame in frames],
        count=len(frames),
        limit=limit,
        after=after,
    )


@admin_app.get("/api/responses/live")
async def live_updates(db: ResponsesDB = Depends(get_db)) -> StreamingResponse:
    async def event_generator() -> AsyncIterator[str]:
        try:
            async for event in db.subscribe_events():
                yield f"data: {event.model_dump_json()}\n\n"
        except asyncio.CancelledError:
            raise

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@admin_app.get("/api/keys", response_model=APIKeyListModel)
async def list_keys(db: ResponsesDB = Depends(get_db)) -> APIKeyListModel:
    items = await db.list_api_keys()
    return APIKeyListModel(items=[_to_api_key_model(item) for item in items], count=len(items))


@admin_app.post("/api/keys", response_model=CreateKeyResponse)
async def create_key(
    payload: CreateKeyRequest, db: ResponsesDB = Depends(get_db)
) -> CreateKeyResponse:
    try:
        token, record = await db.create_api_key(payload.name, alias=payload.alias)
    except UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="API key name already exists") from exc
    return CreateKeyResponse(token=token, record=_to_api_key_model(record))


@admin_app.post("/api/keys/{key_id}/revoke", response_model=RevokeKeyResponse)
async def revoke_key(key_id: UUID, db: ResponsesDB = Depends(get_db)) -> RevokeKeyResponse:
    success = await db.revoke_api_key(key_id)
    if not success:
        raise HTTPException(status_code=404, detail="API key not found or already revoked")
    return RevokeKeyResponse(ok=True)


@admin_app.get("/api/upstream-keys", response_model=UpstreamKeyListModel)
async def list_upstream_keys() -> UpstreamKeyListModel:
    return UpstreamKeyListModel(items=_load_upstream_keys())


# ---------------------------------------------------------------------------
# Static admin UI
# ---------------------------------------------------------------------------

_UI_DIR = Path(__file__).resolve().parent / "admin_ui_dist"
if _UI_DIR.exists():
    admin_app.mount("/", StaticFiles(directory=_UI_DIR, html=True), name="admin-ui")
else:

    @admin_app.get("/")
    def ui_placeholder() -> dict[str, str]:
        return {"message": "Admin UI build not found. Run npm run build in admin UI project."}
