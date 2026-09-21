"""FastAPI history/payload endpoints and an alternating two-instance Electric proxy."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import asyncpg
import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.staticfiles import StaticFiles

TOKEN_SCOPES = {"derisk-test-token": frozenset({"alpha-small", "alpha-large", "race-thread"})}
MAX_PAGE_SIZE = 30
_MAX_PG_BIGINT = 9_223_372_036_854_775_807
_QUERY_PROTOCOL_KEYS = {
    "offset",
    "handle",
    "live",
    "log",
    "replica",
    "cursor",
    "live_sse",
    "subset__where",
    "subset__params",
    "subset__limit",
    "subset__offset",
    "subset__order_by",
    "expired_handle",
    "experimental_live_sse",
}
_RESERVED_SHAPE_KEYS = {"table", "where", "columns", "queryable_columns", "params", "order_by", "limit"}
_ALLOWED_COLUMNS = (
    "conversation_id,row_key,entity_kind,anchor,revision,item_id,item_kind,tool_name,"
    "text_revision,arguments_revision,output_revision,reasoning_revision,"
    "text_bytes,arguments_bytes,output_bytes,reasoning_bytes,status,model,command_id"
)
_WHERE_COMPARISON = re.compile(
    r"\s*(?:\"?(?:anchor|entity_kind|row_key)\"?)\s*(?:=|<>|!=|<=|>=|<|>)\s*"
    r"(?:-?[0-9]+|'(?:[^']|'')*'|\$[1-9][0-9]*)\s*",
    re.IGNORECASE,
)
_WHERE_JOIN = re.compile(r"\s+(?:AND|OR)\s+", re.IGNORECASE)
_ORDER_BY = re.compile(r"\s*\"?anchor\"?\s+(?:ASC|DESC)(?:\s+NULLS\s+(?:FIRST|LAST))?\s*", re.IGNORECASE)
_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
}


def _scope(authorization: str | None, conversation_id: str) -> None:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    token = authorization.removeprefix("Bearer ")
    if conversation_id not in TOKEN_SCOPES.get(token, frozenset()):
        raise HTTPException(status_code=403, detail="Conversation is outside this token scope")


def _cursor(value: str, label: str) -> int:
    if not _INTEGER.fullmatch(value):
        raise HTTPException(status_code=422, detail=f"{label} must be an unsigned decimal string")
    parsed = int(value)
    if parsed > _MAX_PG_BIGINT:
        raise HTTPException(status_code=422, detail=f"{label} exceeds PostgreSQL bigint")
    return parsed


def _validate_where(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 1024:
        raise HTTPException(status_code=400, detail="Unsupported subset WHERE")
    if value.strip().casefold() == "true = true":
        return value
    parts = _WHERE_JOIN.split(value.strip())
    if not parts or any(not _WHERE_COMPARISON.fullmatch(part.strip().strip("() ")) for part in parts):
        raise HTTPException(status_code=400, detail="Subset WHERE may compare only anchor, entity_kind, and row_key")
    return value


def _validate_subset(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Subset request must be a JSON object")
    allowed = {"where", "params", "limit", "offset", "order_by"}
    if set(body) - allowed:
        raise HTTPException(status_code=400, detail="Subset request contains an unsupported shape override")
    subset: dict[str, Any] = {}
    where = _validate_where(body.get("where"))
    if where is not None:
        subset["where"] = where
    params = body.get("params")
    if params is not None:
        if not isinstance(params, (dict, list)) or len(params) > 16:
            raise HTTPException(status_code=400, detail="Subset parameters must be a small scalar collection")
        values = params.values() if isinstance(params, dict) else params
        if any(not isinstance(value, (str, int, float, bool)) and value is not None for value in values):
            raise HTTPException(status_code=400, detail="Nested subset parameters are not supported")
        subset["params"] = params
    limit = body.get("limit")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
            raise HTTPException(status_code=400, detail=f"Subset limit must be in 1..{MAX_PAGE_SIZE}")
        subset["limit"] = limit
    offset = body.get("offset")
    if offset not in (None, 0):
        raise HTTPException(status_code=400, detail="Offset pagination is not supported")
    order_by = body.get("order_by")
    if order_by is not None:
        if isinstance(order_by, str):
            if not _ORDER_BY.fullmatch(order_by):
                raise HTTPException(status_code=400, detail="Subset ordering may use only anchor")
        elif isinstance(order_by, list):
            if not order_by or any(
                not isinstance(item, dict)
                or item.get("column") != "anchor"
                or str(item.get("direction", "")).upper() not in {"ASC", "DESC"}
                for item in order_by
            ):
                raise HTTPException(status_code=400, detail="Subset ordering may use only anchor")
        else:
            raise HTTPException(status_code=400, detail="Unsupported subset ordering")
        subset["order_by"] = order_by
    return subset


def _validate_get_subset(query: Any) -> dict[str, Any]:
    """Validate Electric's legacy subset__* GET parameters before forwarding."""
    raw: dict[str, Any] = {}
    aliases = {
        "subset__where": "where",
        "subset__params": "params",
        "subset__limit": "limit",
        "subset__offset": "offset",
        "subset__order_by": "order_by",
    }
    for source_key, target_key in aliases.items():
        values = query.getlist(source_key)
        if len(values) > 1:
            raise HTTPException(status_code=400, detail=f"Duplicate subset parameter: {source_key}")
        if not values:
            continue
        value: Any = values[0]
        if target_key in {"limit", "offset"}:
            if not _INTEGER.fullmatch(value):
                raise HTTPException(status_code=400, detail=f"Subset {target_key} must be an unsigned integer")
            value = int(value)
        elif target_key == "params":
            try:
                value = json.loads(value)
            except json.JSONDecodeError as error:
                raise HTTPException(status_code=400, detail="Subset parameters must be JSON") from error
        raw[target_key] = value
    return _validate_subset(raw)


async def _forward_electric(request: Request, electric_url: str, instance_name: str) -> Response:
    conversation_id = request.path_params["conversation_id"]
    _scope(request.headers.get("authorization"), conversation_id)
    query = request.query_params
    reserved = _RESERVED_SHAPE_KEYS.intersection(query.keys())
    if reserved:
        raise HTTPException(status_code=400, detail=f"Caller cannot override shape keys: {sorted(reserved)}")
    incoming = {key: value for key, value in query.multi_items() if key in _QUERY_PROTOCOL_KEYS}
    if set(query.keys()) - _QUERY_PROTOCOL_KEYS:
        raise HTTPException(status_code=400, detail="Unsupported Electric protocol query parameter")
    for key, expected in (("replica", "full"), ("log", "changes_only")):
        if key in incoming and incoming[key] != expected:
            raise HTTPException(status_code=400, detail=f"Electric {key} must be {expected}")
    incoming["replica"] = "full"
    fixed = {
        "table": "sync_view_row",
        "where": f"conversation_id = '{conversation_id}'",
        "columns": _ALLOWED_COLUMNS,
        # Electric requires primary-key columns in queryable_columns and only
        # permits projected `columns` from that allow-list. The proxy's subset
        # grammar still rejects caller-supplied conversation_id predicates,
        # while the fixed shape WHERE remains the auth boundary.
        "queryable_columns": _ALLOWED_COLUMNS,
    }
    if "log" not in incoming:
        incoming["log"] = "changes_only"
    params = {**fixed, **incoming}
    body = await request.body()
    if request.method == "POST":
        subset = _validate_subset(json.loads(body or b"{}"))
        body = json.dumps(subset, separators=(",", ":")).encode()
    elif body:
        raise HTTPException(status_code=400, detail="Electric GET requests cannot carry a body")
    else:
        _validate_get_subset(query)
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() in {"accept", "electric-protocol-version", "content-type", "if-none-match"}
    }
    async with httpx.AsyncClient(timeout=30) as client:
        upstream = await client.request(request.method, electric_url, params=params, content=body, headers=headers)
    response_headers = {name: value for name, value in upstream.headers.items() if name.lower() not in _HOP_HEADERS}
    response_headers["x-proxy-instance"] = instance_name
    return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers)


def create_electric_proxy(electric_url: str, instance_name: str) -> FastAPI:
    app = FastAPI()
    app.state.instance_name = instance_name
    app.state.electric_url = electric_url
    app.state.request_count = 0
    app.state.subset_gate = None

    @app.api_route("/shape/{conversation_id}", methods=["GET", "POST"])
    async def proxy_shape(conversation_id: str, request: Request) -> Response:
        app.state.request_count += 1
        response = await _forward_electric(request, app.state.electric_url, instance_name)
        gate: SubsetGate | None = app.state.subset_gate
        is_subset = request.method == "POST" or any(key.startswith("subset__") for key in request.query_params)
        if is_subset and gate is not None and gate.active:
            gate.response_body = bytes(response.body)
            gate.arrived.set()
            await gate.release.wait()
            gate.active = False
        return response

    return app


@dataclass
class SubsetGate:
    active: bool = False
    arrived: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    response_body: bytes = b""


class RoundRobin:
    def __init__(self, backends: list[str]):
        if len(backends) < 2:
            raise ValueError("The spike requires two independent proxy backends")
        self.backends = backends
        self.index = 0
        self.lock = asyncio.Lock()
        self.dispatches: list[str] = []

    async def choose(self) -> tuple[str, str]:
        async with self.lock:
            index = self.index
            self.index = (self.index + 1) % len(self.backends)
            name = f"proxy-{index + 1}"
            self.dispatches.append(name)
            return self.backends[index], name


def _public_row(record: asyncpg.Record | dict[str, Any]) -> dict[str, Any]:
    row = dict(record)
    for key in (
        "anchor",
        "revision",
        "text_revision",
        "arguments_revision",
        "output_revision",
        "reasoning_revision",
        "text_bytes",
        "arguments_bytes",
        "output_bytes",
        "reasoning_bytes",
    ):
        if row[key] is not None:
            row[key] = str(row[key])
    return row


def create_gateway(pool: asyncpg.Pool, electric_backends: list[str], frontend_directory: Path | None = None) -> FastAPI:
    app = FastAPI()
    app.state.round_robin = RoundRobin(electric_backends)
    payload_requests: list[dict[str, Any]] = []
    app.state.payload_requests = payload_requests

    @app.api_route("/api/electric/{conversation_id}", methods=["GET", "POST"])
    async def gateway_shape(conversation_id: str, request: Request) -> Response:
        backend, name = await app.state.round_robin.choose()
        body = await request.body()
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() in {"authorization", "accept", "content-type", "electric-protocol-version"}
        }
        async with httpx.AsyncClient(timeout=35) as client:
            upstream = await client.request(
                request.method,
                f"{backend}/shape/{conversation_id}",
                params=list(request.query_params.multi_items()),
                content=body,
                headers=headers,
            )
        response_headers = {key: value for key, value in upstream.headers.items() if key.lower() not in _HOP_HEADERS}
        response_headers["x-gateway-dispatch"] = name
        return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers)

    @app.get("/api/history/{conversation_id}")
    async def history_page(
        conversation_id: str,
        request: Request,
        before: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query()] = MAX_PAGE_SIZE,
    ) -> dict[str, Any]:
        _scope(request.headers.get("authorization"), conversation_id)
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise HTTPException(status_code=422, detail=f"Limit must be in 1..{MAX_PAGE_SIZE}")
        columns = "conversation_id,row_key,entity_kind,anchor,revision,item_id,item_kind,tool_name,text_revision,arguments_revision,output_revision,reasoning_revision,text_bytes,arguments_bytes,output_bytes,reasoning_bytes,status,model,command_id"
        if before is None:
            records = await pool.fetch(
                f"SELECT {columns} FROM sync_view_row WHERE conversation_id = $1 ORDER BY anchor DESC, row_key LIMIT $2",
                conversation_id,
                limit,
            )
        else:
            cursor = _cursor(before, "before")
            records = await pool.fetch(
                f"SELECT {columns} FROM sync_view_row WHERE conversation_id = $1 AND anchor < $2 ORDER BY anchor DESC, row_key LIMIT $3",
                conversation_id,
                cursor,
                limit,
            )
        return {"rows": [_public_row(record) for record in records]}

    @app.get("/api/payloads/{conversation_id}/{item_id}/{field_name}")
    async def read_payload(
        conversation_id: str, item_id: str, field_name: str, request: Request, after: Annotated[str, Query()] = "0"
    ) -> dict[str, Any]:
        _scope(request.headers.get("authorization"), conversation_id)
        if field_name not in {"text", "arguments", "output", "reasoning"}:
            raise HTTPException(status_code=404, detail="Unknown payload field")
        after_cursor = _cursor(after, "after")
        records = await pool.fetch(
            """SELECT source_cursor, operation, content FROM projected_payload_part
               WHERE conversation_id = $1 AND item_id = $2 AND field_name = $3 AND source_cursor > $4
               ORDER BY source_cursor LIMIT 1000""",
            conversation_id,
            item_id,
            field_name,
            after_cursor,
        )
        rows = [
            {
                "sourceCursor": str(record["source_cursor"]),
                "operation": record["operation"],
                "content": record["content"],
            }
            for record in records
        ]
        app.state.payload_requests.append(
            {
                "conversationId": conversation_id,
                "itemId": item_id,
                "field": field_name,
                "after": str(after_cursor),
                "count": len(rows),
            }
        )
        return {"rows": rows}

    @app.get("/api/checkpoint/{conversation_id}")
    async def get_checkpoint(conversation_id: str, request: Request) -> dict[str, str]:
        _scope(request.headers.get("authorization"), conversation_id)
        record = await pool.fetchrow(
            "SELECT source_id, through_cursor FROM projection_checkpoint WHERE conversation_id = $1", conversation_id
        )
        if record is None:
            raise HTTPException(status_code=404, detail="No projection checkpoint")
        return {"sourceId": record["source_id"], "throughCursor": str(record["through_cursor"])}

    @app.get("/api/proxy-metrics")
    async def proxy_metrics(request: Request, authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        _scope(authorization, "alpha-large")
        return {
            "dispatches": list(app.state.round_robin.dispatches),
            "payloadRequests": list(app.state.payload_requests),
        }

    if frontend_directory is not None:
        app.mount("/", StaticFiles(directory=str(frontend_directory), html=True), name="frontend")
    return app
