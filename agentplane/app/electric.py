"""Authenticated, server-scoped access to bounded Electric conversation shapes."""

from __future__ import annotations

import json
import logging
import time
from typing import Annotated
from uuid import UUID

import anyio
import httpx
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.types import Receive, Scope, Send

from agentplane.app.trajectory import (
    ConversationEntityInterest,
    ConversationInterestExpiredError,
    ConversationScope,
    TrajectoryStore,
)

logger = logging.getLogger(__name__)

_PAGE_SIZE = 30
_SEGMENT_KINDS = "'item','confirmed_input','lifecycle'"
_ENTITY_COLUMNS = (
    "thread_id,source_id,projection_epoch,entity_kind,entity_id,cursor,revision_cursor,pending,turn_id,state,"
    "text_ref,arguments_ref,output_ref,input_ref"
)
_CHUNK_COLUMNS = "thread_id,source_id,projection_epoch,owner_cursor,owner_id,field,generation,chunk_index,text"
# What the conversation page always renders, and so what the window carries without being asked:
# an item's text (a reasoning item's included) and a user's confirmed input. Tool arguments and
# outputs sit behind a disclosure and can be far larger than everything else in a window put
# together, so they stay a read of their own, as does a pending command's input.
_WINDOW_FIELDS = "'text','confirmed_input'"
_PASSTHROUGH_QUERY = frozenset({"offset", "handle", "live", "cursor", "log"})
_SUBSET_QUERY = frozenset({"subset__where", "subset__params"})
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
        "command_id",
    }
)
_RESPONSE_HEADERS = frozenset(
    {
        "cache-control",
        "content-encoding",
        "content-type",
        "electric-cursor",
        "electric-has-data",
        "electric-handle",
        "electric-internal-known-error",
        "electric-offset",
        "electric-schema",
        "electric-snapshot",
        "electric-up-to-date",
        "etag",
        "retry-after",
        "vary",
    }
)


class EntityInterestResponse(BaseModel):
    source_id: str
    projection_epoch: str
    through_cursor: str
    anchor_cursor: str
    tail_from: str
    window_from: str | None
    window_before: str | None


class ElectricStreamingResponse(StreamingResponse):
    def __init__(self, upstream: httpx.Response, headers: dict[str, str]) -> None:
        super().__init__(upstream.aiter_raw(), status_code=upstream.status_code, headers=headers)
        self._upstream = upstream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Disconnect can interrupt send while the body iterator is suspended at yield.
            # Own cleanup at the response boundary, including AnyIO cancellation scopes.
            with anyio.CancelScope(shield=True):
                await self._upstream.aclose()


class ElectricProxy:
    def __init__(self, client: httpx.AsyncClient, store: TrajectoryStore) -> None:
        self._client = client
        self._store = store

    async def commands(
        self, request: Request, thread_id: UUID, source_id: str, projection_epoch: str, command_ids: list[str]
    ) -> StreamingResponse:
        if not command_ids or len(command_ids) > 128 or any(not command_id for command_id in command_ids):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "select between 1 and 128 nonempty command IDs")
        scope = await self._store.current_conversation_scope(thread_id)
        if scope is None or (scope.source_id, scope.projection_epoch) != (source_id, projection_epoch):
            raise HTTPException(status.HTTP_410_GONE, "the selected conversation scope is unavailable")
        params = {"1": str(thread_id), "2": source_id, "3": projection_epoch}
        selected = sorted(set(command_ids))
        params.update({str(index): command_id for index, command_id in enumerate(selected, start=4)})
        placeholders = ",".join(f"${index}" for index in range(4, 4 + len(selected)))
        return await self._forward(
            request,
            table="conversation_entity",
            columns=_ENTITY_COLUMNS,
            where=(
                "thread_id = $1 AND source_id = $2 AND projection_epoch = $3 AND "
                f"entity_kind = 'command' AND entity_id IN ({placeholders})"
            ),
            params=params,
        )

    async def entity_interest(
        self, thread_id: UUID, anchor_cursor: int | None, before_cursor: int | None
    ) -> ConversationEntityInterest:
        try:
            interest = await self._store.conversation_entity_interest(
                thread_id, anchor_cursor=anchor_cursor, before_cursor=before_cursor, page_size=_PAGE_SIZE
            )
        except ConversationInterestExpiredError as error:
            # Electric owns 409/must-refetch; an expired app interest needs new bounds.
            raise HTTPException(status.HTTP_410_GONE, str(error)) from error
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        if interest is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no conversation for thread {thread_id}")
        return interest

    async def payload_generation(
        self,
        thread_id: UUID,
        source_id: str,
        projection_epoch: str,
        owner_cursor: int,
        owner_id: str,
        field: str,
        generation: int,
    ) -> ConversationScope:
        scope = await self._store.conversation_payload_generation(
            thread_id, owner_cursor=owner_cursor, owner_id=owner_id, field=field, generation=generation
        )
        if scope is None or (scope.source_id, scope.projection_epoch) != (source_id, projection_epoch):
            raise HTTPException(status.HTTP_410_GONE, "the selected payload generation is unavailable")
        return scope

    async def _window(
        self,
        thread_id: UUID,
        source_id: str,
        projection_epoch: str,
        anchor_cursor: int,
        tail_from: int,
        window_from: int | None,
        window_before: int | None,
        column: str,
    ) -> tuple[str, dict[str, str]]:
        """The server's own bound on what a reader may see, as a predicate over `column`.

        The entity shape and the content shape take it over `cursor` and `owner_cursor`: a body is
        in a reader's window exactly when the entity that names it is, so resolving the bound once
        is what keeps the two from disagreeing about which those are.
        """
        current_scope = await self._store.current_conversation_scope(thread_id)
        if current_scope is None or (current_scope.source_id, current_scope.projection_epoch) != (
            source_id,
            projection_epoch,
        ):
            raise HTTPException(status.HTTP_410_GONE, "the selected conversation scope is unavailable")
        expected = await self.entity_interest(thread_id, anchor_cursor, window_before)
        if (expected.scope.source_id, expected.scope.projection_epoch) != (source_id, projection_epoch):
            raise HTTPException(status.HTTP_410_GONE, "the selected conversation scope is unavailable")
        if tail_from != expected.tail_from or window_from != expected.window_from:
            raise HTTPException(status.HTTP_410_GONE, "conversation interest has changed; resolve it again")
        params = {
            "1": str(thread_id),
            "2": expected.scope.source_id,
            "3": expected.scope.projection_epoch,
            "4": str(tail_from),
        }
        if window_from is None or window_before is None:
            return f"{column} >= $4", params
        params.update({"5": str(window_from), "6": str(window_before)})
        return f"({column} >= $4 OR ({column} >= $5 AND {column} < $6))", params

    async def entities(
        self,
        request: Request,
        *,
        thread_id: UUID,
        source_id: str,
        projection_epoch: str,
        anchor_cursor: int,
        tail_from: int,
        window_from: int | None,
        window_before: int | None,
    ) -> StreamingResponse:
        bound, params = await self._window(
            thread_id, source_id, projection_epoch, anchor_cursor, tail_from, window_from, window_before, "cursor"
        )
        where = (
            "thread_id = $1 AND source_id = $2 AND projection_epoch = $3 AND ("
            f"(entity_kind IN ({_SEGMENT_KINDS}) AND {bound}) OR entity_kind IN ('view_state','controls') OR "
            "(entity_kind = 'command' AND pending = TRUE))"
        )
        return await self._forward(
            request, table="conversation_entity", columns=_ENTITY_COLUMNS, where=where, params=params
        )

    async def content(
        self,
        request: Request,
        *,
        thread_id: UUID,
        source_id: str,
        projection_epoch: str,
        anchor_cursor: int,
        tail_from: int,
        window_from: int | None,
        window_before: int | None,
    ) -> StreamingResponse:
        """Every rendered body in the entity window, under the entity window's own bound.

        One shape per window rather than one per body: the bodies a reader is about to render are
        the items it is about to render, so they share a partition and a reader opening the same
        tail twice asks for the shape it already has. A body's own extent travels on its
        `PayloadRef`, which is what makes a chunk arriving ahead of the entity naming it harmless.
        """
        bound, params = await self._window(
            thread_id, source_id, projection_epoch, anchor_cursor, tail_from, window_from, window_before, "owner_cursor"
        )
        return await self._forward(
            request,
            table="conversation_payload_chunk",
            columns=_CHUNK_COLUMNS,
            where=(
                f"thread_id = $1 AND source_id = $2 AND projection_epoch = $3 AND {bound} AND "
                f"field IN ({_WINDOW_FIELDS})"
            ),
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
    ) -> StreamingResponse:
        """One body a reader asked for by name: a disclosure it expanded, a command's input.

        The shape names a generation and no revision within it. Chunks are append-only there, so a
        revision-bounded prefix would define a distinct shape per revision and make every growing
        body churn the server's shape cache for bytes the reader already holds.
        """
        await self.payload_generation(thread_id, source_id, projection_epoch, owner_cursor, owner_id, field, generation)
        return await self._forward(
            request,
            table="conversation_payload_chunk",
            columns=_CHUNK_COLUMNS,
            where=(
                "thread_id = $1 AND source_id = $2 AND projection_epoch = $3 AND owner_cursor = $4 AND "
                "owner_id = $5 AND field = $6 AND generation = $7"
            ),
            params={
                "1": str(thread_id),
                "2": source_id,
                "3": projection_epoch,
                "4": str(owner_cursor),
                "5": owner_id,
                "6": field,
                "7": str(generation),
            },
        )

    async def _forward(
        self, request: Request, *, table: str, columns: str, where: str, params: dict[str, str]
    ) -> StreamingResponse:
        # Mutable rows bootstrap from a current snapshot. Replaying a full shape log
        # would make reload cost proportional to the number of past revisions.
        log_mode = "changes_only" if table == "conversation_entity" else "full"
        subset_keys = _SUBSET_QUERY if log_mode == "changes_only" else frozenset()
        allowed = _PASSTHROUGH_QUERY | subset_keys
        if rejected := set(request.query_params) - allowed - _INTEREST_QUERY:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unsupported sync parameters: {sorted(rejected)}")
        for key in allowed:
            if len(request.query_params.getlist(key)) > 1:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"duplicate sync parameter: {key}")
        if (log := request.query_params.get("log")) is not None and log != log_mode:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"the selected sync log must be {log_mode}")
        if (
            "subset__where" in request.query_params
            and request.query_params["subset__where"].strip().casefold() != "true = true"
        ):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "snapshots must select the whole fixed interest")
        if "subset__params" in request.query_params:
            try:
                subset_params = json.loads(request.query_params["subset__params"])
            except json.JSONDecodeError as error:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "subset parameters must be JSON") from error
            if subset_params not in ({}, []):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "snapshots do not accept caller parameters")
        query: list[tuple[str, str | int | float | bool | None]] = [
            (key, value) for key, value in request.query_params.multi_items() if key in allowed and key != "log"
        ]
        query.extend([("table", table), ("columns", columns), ("where", where), ("replica", "full"), ("log", log_mode)])
        if log_mode == "changes_only":
            query.append(("queryable_columns", columns))
        query.extend((f"params[{index}]", value) for index, value in params.items())
        upstream = self._client.build_request(
            "GET",
            "/v1/shape",
            params=httpx.QueryParams(query),
            headers={
                "accept": request.headers.get("accept", "application/json"),
                **{
                    key: request.headers[key]
                    # if-none-match is what lets Electric answer a re-read of an offset it has
                    # already served with 304 and no body; without it its entity tag is inert.
                    for key in ("electric-protocol-version", "if-none-match")
                    if key in request.headers
                },
            },
        )
        # Electric answers headers once the shape exists, so this separates creating a shape from
        # transferring it: a cold creation and a warm snapshot are indistinguishable in a HAR.
        started = time.monotonic()
        try:
            response = await self._client.send(upstream, stream=True)
        except httpx.RequestError as error:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "conversation sync is unavailable") from error
        upstream_seconds = time.monotonic() - started
        logger.info(
            "electric shape response: %s",
            f"{table=} {upstream_seconds=:.3f} status={response.status_code} "
            f"handle={response.headers.get('electric-handle')} live={request.query_params.get('live')}",
        )

        headers = {name: value for name, value in response.headers.items() if name.lower() in _RESPONSE_HEADERS}
        # Electric serves an immutable log segment per offset and marks it publicly cacheable for a
        # long time, which is how the protocol avoids re-transferring history. These responses are
        # caller-scoped, so `public` cannot stand and neither can a `max-age`: a browser's HTTP
        # cache outlives a logout and offers no way to clear it, so a stored body must never be
        # served without a request this proxy authorizes. `no-cache` keeps the body in that cache
        # and forces exactly such a request, and Electric's own entity tag then answers it 304.
        headers["cache-control"] = "private, no-cache"
        return ElectricStreamingResponse(response, headers)


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
    source_id: str,
    projection_epoch: str,
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
        source_id=source_id,
        projection_epoch=projection_epoch,
        anchor_cursor=anchor_cursor,
        tail_from=tail_from,
        window_from=window_from,
        window_before=window_before,
    )


@router.get("/content")
async def get_content(
    request: Request,
    thread_id: UUID,
    source_id: str,
    projection_epoch: str,
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
    return await _proxy(request).content(
        request,
        thread_id=thread_id,
        source_id=source_id,
        projection_epoch=projection_epoch,
        anchor_cursor=anchor_cursor,
        tail_from=tail_from,
        window_from=window_from,
        window_before=window_before,
    )


@router.get("/commands")
async def get_commands(
    request: Request,
    thread_id: UUID,
    source_id: str,
    projection_epoch: str,
    command_id: Annotated[list[str], Query(min_length=1, max_length=128)],
) -> StreamingResponse:
    return await _proxy(request).commands(request, thread_id, source_id, projection_epoch, command_id)


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
    )
