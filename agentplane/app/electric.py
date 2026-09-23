"""Authenticated access to a thread's Electric shapes.

Each thread has one shape over its rows and one per payload field over the bodies' chunks, both
fixed by the server to the thread and its projection epoch. A shape's predicate never moves: a
reader loads its window — the tail, older rows, the bodies in view — as subset snapshots of it,
which Electric ANDs with the shape's own predicate. Those subsets are checked against a short list
of forms, so a reader chooses which rows of its thread it reads, never how many or which thread.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Annotated
from uuid import UUID

import anyio
import httpx
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import Receive, Scope, Send

from agentplane.app.agent_runtime.view.content import ContentStore
from agentplane.app.agent_runtime.view.fold import PayloadField
from agentplane.app.agent_runtime.view.views import EntityKind

logger = logging.getLogger(__name__)

_ENTITY_COLUMNS = (
    "thread_id,projection_epoch,entity_kind,entity_id,entity_index,cursor,revision_cursor,pending,turn_id,state,"
    "text_ref,arguments_ref,output_ref,input_ref"
)
_CHUNK_COLUMNS = "thread_id,projection_epoch,owner_cursor,owner_id,field,generation,chunk_index,text"
# Electric's own protocol parameters, including the two its client adds when recovering a handle.
_PASSTHROUGH_QUERY = frozenset({"offset", "handle", "live", "cursor", "log", "expired_handle", "cache-buster"})
_APP_QUERY = frozenset({"projection_epoch"})
# Rows per subset read; a reader pages further back one read at a time.
SUBSET_ROW_LIMIT = 200
# Bodies per subset read.
SUBSET_BODY_LIMIT = 100
# Positions, newest first. Electric requires an order wherever there is a limit.
_ENTITY_ORDER = "entity_index DESC"
_ENTITY_SUBSETS = {
    # The tail, and each page before a held row.
    None,
    "entity_index < $1",
    # The thread's header, and the commands a reader is waiting on or sent.
    f"entity_kind = '{EntityKind.VIEW_STATE}'",
    f"entity_kind = '{EntityKind.COMMAND}' AND pending = true",
    f"entity_kind = '{EntityKind.COMMAND}' AND entity_id = ANY($1)",
}
_BODY = re.compile(r"\(owner_id = \$\d+ AND generation = \$\d+\)")
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


class ThreadScopeResponse(BaseModel):
    projection_epoch: str = Field(description="The epoch a reader names on every shape request; a stale one gets 410.")
    through_cursor: str = Field(description="The last event the fold has applied; the reader is caught up at it.")


class SubsetRequest(BaseModel):
    """An Electric subset snapshot, as its client sends one."""

    model_config = ConfigDict(extra="forbid")

    where: str | None = None
    params: dict[str, str] | None = None
    order_by: str | None = None
    limit: int | None = None


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


def _check_entity_subset(subset: SubsetRequest) -> None:
    if subset.where not in _ENTITY_SUBSETS or subset.order_by != _ENTITY_ORDER:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unsupported subset: {subset.where=} {subset.order_by=}")
    if subset.limit is None or not 1 <= subset.limit <= SUBSET_ROW_LIMIT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"a subset reads between 1 and {SUBSET_ROW_LIMIT} rows")
    params = subset.params or {}
    if set(params) != ({"1"} if subset.where is not None and "$1" in subset.where else set()):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "subset parameters do not match its form")
    if subset.where == "entity_index < $1" and not params["1"].isdigit():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "an index bound is a non-negative integer")


def _check_body_subset(subset: SubsetRequest) -> None:
    where = subset.where or ""
    bodies = len(_BODY.findall(where))
    # Rebuilt from the count and compared whole, so nothing but these pairs can ride along.
    expected = " OR ".join(
        f"(owner_id = ${2 * index - 1} AND generation = ${2 * index})" for index in range(1, bodies + 1)
    )
    if not bodies or where != expected or subset.order_by is not None or subset.limit is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "a body subset names (owner_id, generation) pairs")
    if bodies > SUBSET_BODY_LIMIT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"a subset reads at most {SUBSET_BODY_LIMIT} bodies")
    params = subset.params or {}
    if set(params) != {str(index) for index in range(1, 2 * bodies + 1)}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "subset parameters do not match its form")
    if not all(params[str(2 * index)].isdigit() for index in range(1, bodies + 1)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "a generation is a non-negative integer")


class ElectricProxy:
    def __init__(self, client: httpx.AsyncClient, content: ContentStore) -> None:
        self._client = client
        self._content = content

    async def scope(self, thread_id: UUID) -> ThreadScopeResponse:
        scope = await self._content.current_scope(thread_id)
        if scope is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no materialized fold for thread {thread_id}")
        return ThreadScopeResponse(projection_epoch=scope.projection_epoch, through_cursor=str(scope.through_cursor))

    async def entities(
        self, request: Request, thread_id: UUID, projection_epoch: str, subset: SubsetRequest | None = None
    ) -> StreamingResponse:
        await self._require_epoch(thread_id, projection_epoch)
        if subset is not None:
            _check_entity_subset(subset)
        return await self._forward(
            request,
            table="thread_entity",
            columns=_ENTITY_COLUMNS,
            where="thread_id = $1 AND projection_epoch = $2",
            params={"1": str(thread_id), "2": projection_epoch},
            subset=subset,
        )

    async def chunks(
        self,
        request: Request,
        thread_id: UUID,
        projection_epoch: str,
        field: PayloadField,
        subset: SubsetRequest | None = None,
    ) -> StreamingResponse:
        await self._require_epoch(thread_id, projection_epoch)
        if subset is not None:
            _check_body_subset(subset)
        return await self._forward(
            request,
            table="thread_payload_chunk",
            columns=_CHUNK_COLUMNS,
            where="thread_id = $1 AND projection_epoch = $2 AND field = $3",
            params={"1": str(thread_id), "2": projection_epoch, "3": field},
            subset=subset,
        )

    async def _require_epoch(self, thread_id: UUID, projection_epoch: str) -> None:
        # A rebuilt fold is a new epoch; a reader holding the old one reloads rather than mixing them.
        scope = await self._content.current_scope(thread_id)
        if scope is None or scope.projection_epoch != projection_epoch:
            raise HTTPException(status.HTTP_410_GONE, "the selected thread scope is unavailable")

    async def _forward(
        self,
        request: Request,
        *,
        table: str,
        columns: str,
        where: str,
        params: dict[str, str],
        subset: SubsetRequest | None,
    ) -> StreamingResponse:
        if rejected := set(request.query_params) - _PASSTHROUGH_QUERY - _APP_QUERY:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unsupported sync parameters: {sorted(rejected)}")
        for key in _PASSTHROUGH_QUERY:
            if len(request.query_params.getlist(key)) > 1:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"duplicate sync parameter: {key}")
        # Rows are mutable, so a reader bootstraps from subsets of current state rather than replaying
        # every past revision; that is also what lets a window move inside one shape.
        if (log := request.query_params.get("log")) is not None and log != "changes_only":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "the selected sync log must be changes_only")
        query: list[tuple[str, str | int | float | bool | None]] = [
            (key, value)
            for key, value in request.query_params.multi_items()
            if key in _PASSTHROUGH_QUERY and key != "log"
        ]
        query.extend(
            [
                ("table", table),
                ("columns", columns),
                ("where", where),
                ("replica", "full"),
                ("log", "changes_only"),
                ("queryable_columns", columns),
            ]
        )
        query.extend((f"params[{index}]", value) for index, value in params.items())
        upstream = self._client.build_request(
            "GET" if subset is None else "POST",
            "/v1/shape",
            params=httpx.QueryParams(query),
            json=None if subset is None else subset.model_dump(exclude_none=True),
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
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "thread sync is unavailable") from error
        upstream_seconds = time.monotonic() - started
        logger.info(
            "electric shape response: %s",
            f"{table=} subset={subset is not None} {upstream_seconds=:.3f} status={response.status_code} "
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


router = APIRouter(prefix="/threads/{thread_id}/sync", tags=["thread-sync"])


def _proxy(request: Request) -> ElectricProxy:
    proxy = request.app.state.electric
    if not isinstance(proxy, ElectricProxy):
        raise TypeError(f"app.state.electric is {type(proxy).__name__}, not ElectricProxy")
    return proxy


@router.get("/scope")
async def get_scope(request: Request, thread_id: UUID) -> ThreadScopeResponse:
    return await _proxy(request).scope(thread_id)


@router.get("/entities")
async def get_entities(
    request: Request,
    thread_id: UUID,
    projection_epoch: str,
    offset: Annotated[str | None, Query()] = None,
    handle: Annotated[str | None, Query()] = None,
    live: Annotated[bool | None, Query()] = None,
) -> StreamingResponse:
    del offset, handle, live
    return await _proxy(request).entities(request, thread_id, projection_epoch)


@router.post("/entities")
async def post_entities(
    request: Request, thread_id: UUID, projection_epoch: str, subset: SubsetRequest
) -> StreamingResponse:
    return await _proxy(request).entities(request, thread_id, projection_epoch, subset)


@router.get("/chunks/{field}")
async def get_chunks(
    request: Request,
    thread_id: UUID,
    field: PayloadField,
    projection_epoch: str,
    offset: Annotated[str | None, Query()] = None,
    handle: Annotated[str | None, Query()] = None,
    live: Annotated[bool | None, Query()] = None,
) -> StreamingResponse:
    del offset, handle, live
    return await _proxy(request).chunks(request, thread_id, projection_epoch, field)


@router.post("/chunks/{field}")
async def post_chunks(
    request: Request, thread_id: UUID, field: PayloadField, projection_epoch: str, subset: SubsetRequest
) -> StreamingResponse:
    return await _proxy(request).chunks(request, thread_id, projection_epoch, field, subset)
