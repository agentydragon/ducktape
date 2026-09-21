"""Authenticated, server-scoped access to bounded Electric conversation shapes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agentplane.app.trajectory import ConversationEntityInterest, ConversationPayloadSelection

_PAGE_SIZE = 30
_SEGMENT_KINDS = "'item','confirmed_input','lifecycle'"
_ENTITY_COLUMNS = (
    "thread_id,source_id,projection_epoch,entity_kind,entity_id,cursor,revision_cursor,pending,turn_id,state,"
    "text_ref,arguments_ref,output_ref,input_ref"
)
_CHUNK_COLUMNS = "thread_id,source_id,projection_epoch,owner_cursor,owner_id,field,generation,chunk_index,text"
_PASSTHROUGH_QUERY = frozenset({"offset", "handle", "live"})
_INTEREST_QUERY = frozenset(
    {
        "anchor_cursor",
        "tail_from",
        "window_from",
        "window_before",
        "source_id",
        "projection_epoch",
        "owner_cursor",
        "owner_id",
        "field",
        "generation",
        "revision_cursor",
        "follow",
    }
)
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

EntityInterestResolver = Callable[[UUID, int | None, int | None, int], Awaitable[ConversationEntityInterest | None]]
PayloadResolver = Callable[[UUID, int, str, str, int, int], Awaitable[ConversationPayloadSelection | None]]


class EntityInterestResponse(BaseModel):
    source_id: str
    projection_epoch: str
    through_cursor: str
    anchor_cursor: str
    tail_from: str
    window_from: str | None
    window_before: str | None


class PayloadInterestResponse(BaseModel):
    source_id: str
    projection_epoch: str
    owner_cursor: str
    owner_id: str
    field: str
    generation: str
    revision_cursor: str
    present: bool
    chunk_count: str
    content_bytes: str


class ElectricProxy:
    def __init__(
        self, client: httpx.AsyncClient, resolve_entities: EntityInterestResolver, resolve_payload: PayloadResolver
    ) -> None:
        self._client = client
        self._resolve_entities = resolve_entities
        self._resolve_payload = resolve_payload

    async def entity_interest(
        self, thread_id: UUID, anchor_cursor: int | None, before_cursor: int | None
    ) -> ConversationEntityInterest:
        interest = await self._resolve_entities(thread_id, anchor_cursor, before_cursor, _PAGE_SIZE)
        if interest is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no conversation for thread {thread_id}")
        return interest

    async def payload_selection(
        self,
        thread_id: UUID,
        source_id: str,
        projection_epoch: str,
        owner_cursor: int,
        owner_id: str,
        field: str,
        generation: int,
        revision_cursor: int,
    ) -> ConversationPayloadSelection:
        selection = await self._resolve_payload(thread_id, owner_cursor, owner_id, field, generation, revision_cursor)
        if (
            selection is None
            or selection.scope.source_id != source_id
            or selection.scope.projection_epoch != projection_epoch
        ):
            raise HTTPException(status.HTTP_410_GONE, "the selected payload revision is unavailable")
        return selection

    async def entities(
        self,
        request: Request,
        *,
        thread_id: UUID,
        anchor_cursor: int,
        tail_from: int,
        window_from: int | None,
        window_before: int | None,
    ) -> StreamingResponse:
        expected = await self.entity_interest(thread_id, anchor_cursor, window_before)
        if tail_from != expected.tail_from or window_from != expected.window_from:
            raise HTTPException(status.HTTP_409_CONFLICT, "conversation interest has changed; resolve it again")
        scope = expected.scope
        segment = f"(entity_kind IN ({_SEGMENT_KINDS}) AND cursor >= $4)"
        params: dict[str, str] = {
            "1": str(thread_id),
            "2": scope.source_id,
            "3": scope.projection_epoch,
            "4": str(tail_from),
        }
        if window_from is not None and window_before is not None:
            segment = f"(entity_kind IN ({_SEGMENT_KINDS}) AND (cursor >= $4 OR (cursor >= $5 AND cursor < $6)))"
            params.update({"5": str(window_from), "6": str(window_before)})
        where = (
            "thread_id = $1 AND source_id = $2 AND projection_epoch = $3 AND ("
            f"{segment} OR entity_kind IN ('view_state','controls') OR "
            "(entity_kind = 'command' AND pending = TRUE))"
        )
        return await self._forward(
            request,
            table="conversation_entity",
            columns=_ENTITY_COLUMNS,
            queryable_columns="entity_kind,pending,cursor,entity_id",
            where=where,
            params=params,
        )

    async def payload_chunks(
        self,
        request: Request,
        *,
        thread_id: UUID,
        source_id: str,
        projection_epoch: str,
        owner_cursor: int,
        owner_id: str,
        field: str,
        generation: int,
        revision_cursor: int,
        follow: bool,
    ) -> StreamingResponse:
        selection = await self.payload_selection(
            thread_id, source_id, projection_epoch, owner_cursor, owner_id, field, generation, revision_cursor
        )
        params = {
            "1": str(thread_id),
            "2": source_id,
            "3": projection_epoch,
            "4": str(owner_cursor),
            "5": owner_id,
            "6": field,
            "7": str(generation),
        }
        chunk_bound = ""
        if not follow:
            chunk_bound = " AND chunk_index < $8"
            params["8"] = str(selection.chunk_count)
        return await self._forward(
            request,
            table="conversation_payload_chunk",
            columns=_CHUNK_COLUMNS,
            queryable_columns="owner_cursor,owner_id,field,generation,chunk_index",
            where=(
                "thread_id = $1 AND source_id = $2 AND projection_epoch = $3 AND owner_cursor = $4 AND "
                f"owner_id = $5 AND field = $6 AND generation = $7{chunk_bound}"
            ),
            params=params,
        )

    async def _forward(
        self, request: Request, *, table: str, columns: str, queryable_columns: str, where: str, params: dict[str, str]
    ) -> StreamingResponse:
        if rejected := set(request.query_params) - _PASSTHROUGH_QUERY - _INTEREST_QUERY:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unsupported sync parameters: {sorted(rejected)}")
        query: list[tuple[str, str | int | float | bool | None]] = [
            (key, value) for key, value in request.query_params.multi_items() if key in _PASSTHROUGH_QUERY
        ]
        query.extend(
            [
                ("table", table),
                ("columns", columns),
                ("queryable_columns", queryable_columns),
                ("where", where),
                ("replica", "full"),
            ]
        )
        query.extend((f"params[{index}]", value) for index, value in params.items())
        upstream = self._client.build_request(
            "GET",
            "/v1/shape",
            params=httpx.QueryParams(query),
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


@router.get("/interest")
async def get_interest(
    request: Request, thread_id: UUID, before_cursor: Annotated[int | None, Query(ge=0)] = None
) -> EntityInterestResponse:
    interest = await _proxy(request).entity_interest(thread_id, None, before_cursor)
    return EntityInterestResponse(
        source_id=interest.scope.source_id,
        projection_epoch=interest.scope.projection_epoch,
        through_cursor=str(interest.scope.through_cursor),
        anchor_cursor=str(interest.anchor_cursor),
        tail_from=str(interest.tail_from),
        window_from=str(interest.window_from) if interest.window_from is not None else None,
        window_before=str(interest.window_before) if interest.window_before is not None else None,
    )


@router.get("/entities")
async def get_entities(
    request: Request,
    thread_id: UUID,
    anchor_cursor: Annotated[int, Query(ge=0)],
    tail_from: Annotated[int, Query(ge=0)],
    window_from: Annotated[int | None, Query(ge=0)] = None,
    window_before: Annotated[int | None, Query(ge=0)] = None,
    offset: Annotated[str | None, Query()] = None,
    handle: Annotated[str | None, Query()] = None,
    live: Annotated[bool | None, Query()] = None,
) -> StreamingResponse:
    del offset, handle, live
    if (window_from is None) != (window_before is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "history window bounds must be supplied together")
    return await _proxy(request).entities(
        request,
        thread_id=thread_id,
        anchor_cursor=anchor_cursor,
        tail_from=tail_from,
        window_from=window_from,
        window_before=window_before,
    )


@router.get("/payload-interest")
async def get_payload_interest(
    request: Request,
    thread_id: UUID,
    source_id: str,
    projection_epoch: str,
    owner_cursor: Annotated[int, Query(ge=0)],
    owner_id: str,
    field: str,
    generation: Annotated[int, Query(ge=0)],
    revision_cursor: Annotated[int, Query(ge=0)],
) -> PayloadInterestResponse:
    selection = await _proxy(request).payload_selection(
        thread_id, source_id, projection_epoch, owner_cursor, owner_id, field, generation, revision_cursor
    )
    return PayloadInterestResponse(
        source_id=selection.scope.source_id,
        projection_epoch=selection.scope.projection_epoch,
        owner_cursor=str(selection.owner_cursor),
        owner_id=selection.owner_id,
        field=selection.field,
        generation=str(selection.generation),
        revision_cursor=str(selection.revision_cursor),
        present=selection.present,
        chunk_count=str(selection.chunk_count),
        content_bytes=str(selection.content_bytes),
    )


@router.get("/payload-chunks")
async def get_payload_chunks(
    request: Request,
    thread_id: UUID,
    source_id: str,
    projection_epoch: str,
    owner_cursor: Annotated[int, Query(ge=0)],
    owner_id: str,
    field: str,
    generation: Annotated[int, Query(ge=0)],
    revision_cursor: Annotated[int, Query(ge=0)],
    follow: bool = False,
    offset: Annotated[str | None, Query()] = None,
    handle: Annotated[str | None, Query()] = None,
    live: Annotated[bool | None, Query()] = None,
) -> StreamingResponse:
    del offset, handle, live
    return await _proxy(request).payload_chunks(
        request,
        thread_id=thread_id,
        source_id=source_id,
        projection_epoch=projection_epoch,
        owner_cursor=owner_cursor,
        owner_id=owner_id,
        field=field,
        generation=generation,
        revision_cursor=revision_cursor,
        follow=follow,
    )
