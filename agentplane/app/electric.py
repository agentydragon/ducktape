"""Authenticated, server-scoped access to Electric conversation shapes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConversationShape(StrEnum):
    ENTITIES = "entities"
    PAYLOAD_MANIFESTS = "payload-manifests"
    PAYLOAD_CHUNKS = "payload-chunks"


@dataclass(frozen=True)
class ConversationScope:
    thread_id: UUID
    source_id: str
    projection_epoch: int


class Subset(BaseModel):
    """The intentionally small subset of Electric's POST snapshot language we expose."""

    model_config = ConfigDict(extra="forbid")

    where: str
    params: dict[str, str | list[str]]
    order_by: str
    limit: int = Field(ge=1, le=200)
    offset: int | None = Field(default=None, ge=0, le=10_000)

    @model_validator(mode="after")
    def supported_query(self) -> Subset:
        if (self.where, self.order_by) not in {
            ("segment_cursor < $1", "segment_cursor DESC"),
            ("segment_cursor > $1", "segment_cursor ASC"),
            ("cursor_or_id = ANY($1)", "segment_cursor ASC"),
            ("owner_id = $1 AND field = $2 AND generation = $3 AND revision_cursor = $4", "chunk_index ASC"),
        }:
            raise ValueError("unsupported conversation subset")
        expected = {str(index) for index in range(1, self.where.count("$") + 1)}
        if set(self.params) != expected:
            raise ValueError("subset params do not match the supported query")
        return self


_PASSTHROUGH_QUERY = frozenset({"offset", "handle", "live", "live_sse"})
_RESPONSE_HEADERS = frozenset(
    {
        "cache-control",
        "content-encoding",
        "content-type",
        "electric-cursor",
        "electric-handle",
        "electric-offset",
        "electric-schema",
        "etag",
        "vary",
    }
)
_SHAPE_TABLES = {
    ConversationShape.ENTITIES: "conversation_entity",
    ConversationShape.PAYLOAD_MANIFESTS: "conversation_payload_manifest",
    ConversationShape.PAYLOAD_CHUNKS: "conversation_payload_chunk",
}
_SHAPE_COLUMNS = {
    ConversationShape.ENTITIES: (
        "thread_id,source_id,projection_epoch,kind,cursor_or_id,segment_cursor,identity,revision,payload"
    ),
    ConversationShape.PAYLOAD_MANIFESTS: (
        "thread_id,source_id,projection_epoch,owner_cursor,owner_id,field,generation,revision_cursor,"
        "present,chunk_count,content_bytes"
    ),
    ConversationShape.PAYLOAD_CHUNKS: (
        "thread_id,source_id,projection_epoch,owner_cursor,owner_id,field,generation,revision_cursor,"
        "chunk_index,content"
    ),
}


ScopeResolver = Callable[[UUID], Awaitable[ConversationScope | None]]


class ElectricProxy:
    def __init__(self, client: httpx.AsyncClient, resolve_scope: ScopeResolver) -> None:
        self._client = client
        self._resolve_scope = resolve_scope

    async def forward(
        self, request: Request, *, thread_id: UUID, shape: ConversationShape, subset: Subset | None
    ) -> StreamingResponse:
        scope = await self._resolve_scope(thread_id)
        if scope is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no thread {thread_id}")

        supplied = set(request.query_params)
        if rejected := supplied - _PASSTHROUGH_QUERY:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unsupported sync parameters: {sorted(rejected)}")
        params: list[tuple[str, str]] = [(key, value) for key, value in request.query_params.multi_items()]
        params.extend(
            [
                ("table", _SHAPE_TABLES[shape]),
                ("columns", _SHAPE_COLUMNS[shape]),
                (
                    "queryable_columns",
                    "segment_cursor,cursor_or_id,owner_id,field,generation,revision_cursor,chunk_index",
                ),
                ("where", "thread_id = $1 AND source_id = $2 AND projection_epoch = $3"),
                ("params", json.dumps({"1": str(scope.thread_id), "2": scope.source_id, "3": scope.projection_epoch})),
                ("log", "changes_only"),
                ("replica", "full"),
            ]
        )
        upstream = self._client.build_request(
            request.method,
            "/v1/shape",
            params=params,
            json=subset.model_dump(exclude_none=True) if subset is not None else None,
            headers={"accept": request.headers.get("accept", "application/json")},
        )
        try:
            response = await self._client.send(upstream, stream=True)
        except httpx.RequestError as error:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "conversation sync is unavailable") from error

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()

        headers = {name: value for name, value in response.headers.items() if name.lower() in _RESPONSE_HEADERS}
        return StreamingResponse(body(), status_code=response.status_code, headers=headers)


router = APIRouter(prefix="/threads/{thread_id}/sync", tags=["conversation-sync"])


def _proxy(request: Request) -> ElectricProxy:
    proxy = request.app.state.electric
    if not isinstance(proxy, ElectricProxy):
        raise TypeError(f"app.state.electric is {type(proxy).__name__}, not ElectricProxy")
    return proxy


@router.get("/{shape}")
async def get_shape(
    request: Request,
    thread_id: UUID,
    shape: ConversationShape,
    offset: Annotated[str | None, Query()] = None,
    handle: Annotated[str | None, Query()] = None,
    live: Annotated[bool | None, Query()] = None,
    live_sse: Annotated[bool | None, Query()] = None,
) -> StreamingResponse:
    del offset, handle, live, live_sse
    return await _proxy(request).forward(request, thread_id=thread_id, shape=shape, subset=None)


@router.post("/{shape}")
async def post_subset(
    request: Request,
    thread_id: UUID,
    shape: ConversationShape,
    subset: Subset,
    offset: Annotated[str | None, Query()] = None,
    handle: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    del offset, handle
    return await _proxy(request).forward(request, thread_id=thread_id, shape=shape, subset=subset)
