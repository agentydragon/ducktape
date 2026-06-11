from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from uuid import UUID

from asyncpg import UniqueViolationError
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai.types.responses import ResponseStreamEvent
from pydantic import BaseModel, Field
from pydantic.config import ConfigDict

from openai_utils.model import ResponsesRequest
from rspcache.models import FinalResponseSnapshot, ResponseStatus
from rspcache.responses_db import APIKeyRecord, ClientAPIKey, Response, ResponsesDB

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

    sequence_number: int
    frame_type: str
    created_at: datetime
    typed_frame: ResponseStreamEvent


class ResponseRecordModel(BaseModel):
    """API model for cached OpenAI API responses.

    Attributes:
        cache_key: SHA256 hash of request body (used for cache lookups)
        response_id: OpenAI's response ID (e.g., 'resp_abc123'), nullable
    """

    model_config = ConfigDict(from_attributes=True)

    # Core Response fields
    cache_key: str
    response_id: str | None = None
    model: str
    status: ResponseStatus
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    latency_ms: int | None = None

    request_body: ResponsesRequest

    # Nested relationships (typed)
    api_key: APIKeyModel | None = None
    snapshot: FinalResponseSnapshot | None = None


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
    created_at: datetime
    revoked_at: datetime | None = None


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


def _to_response_model(record: Response) -> ResponseRecordModel:
    """Convert DB Response record to API model."""
    request_payload = record.request_body if record.request_body is not None else {"input": []}
    request_model = ResponsesRequest.model_validate(request_payload)
    return ResponseRecordModel(
        cache_key=record.cache_key,
        response_id=record.response_id,
        model=record.model,
        status=record.status,
        error=record.error,
        created_at=record.created_at,
        updated_at=record.updated_at,
        latency_ms=record.latency_ms,
        request_body=request_model,
        api_key=_to_api_key_model(record.api_key) if record.api_key else None,
        snapshot=record.snapshot.to_model() if record.snapshot else None,
    )


def _to_api_key_model(record: ClientAPIKey | APIKeyRecord) -> APIKeyModel:
    return APIKeyModel(
        id=record.id,
        name=record.name,
        token_prefix=record.token_prefix,
        upstream_alias=record.upstream_alias,
        created_at=record.created_at,
        revoked_at=record.revoked_at,
    )


@admin_app.get("/api/responses", response_model=ResponseListModel)
async def list_responses(
    limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(0, ge=0),
    db: ResponsesDB = Depends(get_db),
) -> ResponseListModel:
    items, total = await db.list_responses(limit=limit, offset=offset)
    return ResponseListModel(
        items=[_to_response_model(item) for item in items], limit=limit, offset=offset, total=total
    )


@admin_app.get("/api/responses/{identifier}", response_model=ResponseRecordModel)
async def get_response(identifier: str, db: ResponsesDB = Depends(get_db)) -> ResponseRecordModel:
    detail = await db.get_response_detail(identifier)
    if detail is None:
        raise HTTPException(status_code=404, detail="Response not found")
    return _to_response_model(detail.record)


@admin_app.get("/api/responses/{identifier}/frames", response_model=FrameListModel)
async def get_frames(
    identifier: str,
    limit: int | None = Query(DEFAULT_FRAME_LIMIT, ge=1, le=1000),
    after: int | None = Query(None, ge=0),
    db: ResponsesDB = Depends(get_db),
) -> FrameListModel:
    frames = await db.get_frames(identifier, limit=limit, after_sequence_number=after)
    return FrameListModel(
        items=[FrameRecordModel.model_validate(frame) for frame in frames], count=len(frames), limit=limit, after=after
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
async def create_key(payload: CreateKeyRequest, db: ResponsesDB = Depends(get_db)) -> CreateKeyResponse:
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
