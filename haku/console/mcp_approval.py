"""Operator-approved MCP tool calls owned by haku-console.

This router is the privileged tool-call ledger: callers can discover connected MCP
servers, submit exact calls against any reflected tool, and read the console-owned
result/audit state. In v1 every execution waits for an operator decision in trusted
console chrome.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, cast

import canonicaljson
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi_csrf_protect import CsrfProtect
from fastmcp.client import Client
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sqlalchemy import create_engine, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from haku.console.config import Settings
from haku.console.database_migrate import run_migrations_for_connection
from haku.console.database_schema import sqlalchemy_url, tool_call_events, tool_calls
from haku.console.deps import SettingsDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp-approval"])
Csrf = Annotated[CsrfProtect, Depends()]


class ToolCallStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    DENIED = "denied"


TERMINAL_STATUSES = {ToolCallStatus.OK, ToolCallStatus.ERROR, ToolCallStatus.DENIED}


class McpServerEntry(BaseModel):
    id: str
    server_url: str
    bearer_token_secret: str | None = None


class McpServerCatalogFile(BaseModel):
    servers: list[McpServerEntry] = Field(default_factory=list)


class ToolMetadata(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    schema_source: Literal["mcp", "unavailable"] = "unavailable"
    degraded_reason: str | None = None


class ServerMetadata(BaseModel):
    server_id: str
    title: str
    tools: list[ToolMetadata] = Field(default_factory=list)
    schema_source: Literal["mcp", "unavailable"] = "unavailable"
    degraded_reason: str | None = None


class ToolCapabilitiesResponse(BaseModel):
    servers: list[ServerMetadata] = Field(default_factory=list)


class SubmitToolCallRequest(BaseModel):
    server_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    title: str | None = None
    client_request_id: str | None = None
    state_request_id: str | None = None
    wait_for_ms: int = Field(default=0, ge=0, le=60_000)


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "deny"]
    reason: str | None = None


class ToolCallRecord(BaseModel):
    tool_call_id: str
    server_id: str
    server_title: str
    tool_name: str
    caller_principal: str
    status: ToolCallStatus
    created_at: dt.datetime
    updated_at: dt.datetime
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    title: str | None = None
    client_request_id: str | None = None
    state_request_id: str | None = None
    request_digest: str
    decision_reason: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class ToolCallDbRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True, use_enum_values=True)

    tool_call_id: str
    server_id: str
    server_title: str
    tool_name: str
    caller_principal: str
    status: ToolCallStatus
    created_at: dt.datetime
    updated_at: dt.datetime
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("arguments_json", "arguments"),
        serialization_alias="arguments_json",
    )
    rationale: str = ""
    title: str | None = None
    client_request_id: str | None = None
    state_request_id: str | None = None
    request_digest: str
    decision_reason: str | None = None
    result: dict[str, Any] | None = Field(
        default=None, validation_alias=AliasChoices("result_json", "result"), serialization_alias="result_json"
    )
    error: str | None = None

    @classmethod
    def from_record(cls, record: ToolCallRecord) -> ToolCallDbRow:
        return cls.model_validate(record.model_dump(mode="python"))

    def to_record(self) -> ToolCallRecord:
        return ToolCallRecord.model_validate(self.model_dump(mode="python", by_alias=False))


class ToolCallEvent(BaseModel):
    event_id: int
    event_type: Literal["tool_call_submitted", "approval_pending", "tool_call_updated"]
    tool_call_id: str
    status: ToolCallStatus
    created_at: dt.datetime


class ToolCallEventDbRow(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    event_id: int
    event_type: Literal["tool_call_submitted", "approval_pending", "tool_call_updated"]
    tool_call_id: str
    status: ToolCallStatus
    created_at: dt.datetime

    def to_event(self) -> ToolCallEvent:
        return ToolCallEvent.model_validate(self.model_dump(mode="python"))


class PendingApproval(BaseModel):
    tool_call_id: str
    server_id: str
    title: str
    server_title: str
    tool_name: str
    caller_principal: str
    rationale: str
    arguments: dict[str, Any]
    created_at: dt.datetime


class PendingApprovalsResponse(BaseModel):
    approvals: list[PendingApproval] = Field(default_factory=list)


class ToolCallListResponse(BaseModel):
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    next_cursor: str | None = None


class ToolCallEventsResponse(BaseModel):
    events: list[ToolCallEvent] = Field(default_factory=list)


class ApprovalDecisionResponse(BaseModel):
    tool_call: ToolCallRecord


class ToolCallStoreProtocol(Protocol):
    def submit(
        self, *, server: McpServerEntry, req: SubmitToolCallRequest, caller_principal: str
    ) -> tuple[ToolCallRecord, list[ToolCallEvent], bool]: ...

    def get(self, tool_call_id: str) -> ToolCallRecord: ...

    def find_by_client_request_id(self, caller_principal: str, client_request_id: str) -> ToolCallRecord: ...

    def list(
        self, *, status: str | None = None, since: int | None = None, limit: int = 100
    ) -> ToolCallListResponse: ...

    def pending_approvals(self) -> PendingApprovalsResponse: ...

    def events_since(self, since: int = 0) -> ToolCallEventsResponse: ...

    def mark_running(self, tool_call_id: str) -> tuple[ToolCallRecord, ToolCallEvent]: ...

    def deny(self, tool_call_id: str, reason: str | None) -> tuple[ToolCallRecord, ToolCallEvent]: ...

    def finish(
        self, tool_call_id: str, *, result: dict[str, Any] | None, error: str | None
    ) -> tuple[ToolCallRecord, ToolCallEvent]: ...


class ToolCallStore:
    """Small durable single-replica JSON ledger.

    Tests and local dev use this store when no database URL is configured. Production
    uses Postgres so approval state is not tied to one pod or one PVC.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._tool_calls: dict[str, ToolCallRecord] = {}
        self._events: list[ToolCallEvent] = []
        self._next_event_id = 1
        if path is not None:
            self._load()

    def _load(self) -> None:
        assert self._path is not None
        if not self._path.exists():
            return
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        self._tool_calls = {
            tool_call_id: ToolCallRecord.model_validate(record)
            for tool_call_id, record in raw.get("tool_calls", {}).items()
        }
        self._events = [ToolCallEvent.model_validate(e) for e in raw.get("events", [])]
        self._next_event_id = int(raw.get("next_event_id", len(self._events) + 1))

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "next_event_id": self._next_event_id,
            "tool_calls": {k: v.model_dump(mode="json") for k, v in self._tool_calls.items()},
            "events": [e.model_dump(mode="json") for e in self._events],
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self._path.parent, delete=False) as f:
            json.dump(payload, f, sort_keys=True)
            tmp = f.name
        Path(tmp).replace(self._path)

    def _append_event(
        self,
        event_type: Literal["tool_call_submitted", "approval_pending", "tool_call_updated"],
        record: ToolCallRecord,
    ) -> ToolCallEvent:
        event = ToolCallEvent(
            event_id=self._next_event_id,
            event_type=event_type,
            tool_call_id=record.tool_call_id,
            status=record.status,
            created_at=dt.datetime.now(dt.UTC),
        )
        self._next_event_id += 1
        self._events.append(event)
        return event

    def submit(
        self, *, server: McpServerEntry, req: SubmitToolCallRequest, caller_principal: str
    ) -> tuple[ToolCallRecord, list[ToolCallEvent], bool]:
        digest = _request_digest(req)
        with self._lock:
            if req.client_request_id is not None:
                existing = self._find_by_client_request_id_unlocked(caller_principal, req.client_request_id)
                if existing is not None:
                    if existing.request_digest != digest:
                        raise HTTPException(
                            status_code=409,
                            detail="client_request_id was already used for a different tool-call payload",
                        )
                    return existing, [], False

            now = dt.datetime.now(dt.UTC)
            status = ToolCallStatus.PENDING_APPROVAL
            record = ToolCallRecord(
                tool_call_id=f"tc_{secrets.token_hex(12)}",
                server_id=server.id,
                server_title=server.id,
                tool_name=req.tool_name,
                caller_principal=caller_principal,
                status=status,
                created_at=now,
                updated_at=now,
                arguments=req.arguments,
                rationale=req.rationale,
                title=req.title,
                client_request_id=req.client_request_id,
                state_request_id=req.state_request_id,
                request_digest=digest,
            )
            self._tool_calls[record.tool_call_id] = record
            events = [self._append_event("tool_call_submitted", record)]
            events.append(self._append_event("approval_pending", record))
            self._save()
            return record, events, True

    def _find_by_client_request_id_unlocked(
        self, caller_principal: str, client_request_id: str
    ) -> ToolCallRecord | None:
        return next(
            (
                record
                for record in self._tool_calls.values()
                if record.caller_principal == caller_principal and record.client_request_id == client_request_id
            ),
            None,
        )

    def get(self, tool_call_id: str) -> ToolCallRecord:
        with self._lock:
            record = self._tool_calls.get(tool_call_id)
            if record is None:
                raise HTTPException(status_code=404, detail="tool call not found")
            return record

    def find_by_client_request_id(self, caller_principal: str, client_request_id: str) -> ToolCallRecord:
        with self._lock:
            record = self._find_by_client_request_id_unlocked(caller_principal, client_request_id)
            if record is None:
                raise HTTPException(status_code=404, detail="tool call not found")
            return record

    def list(self, *, status: str | None = None, since: int | None = None, limit: int = 100) -> ToolCallListResponse:
        with self._lock:
            records = sorted(self._tool_calls.values(), key=lambda r: r.created_at)
            if status == "terminal":
                records = [r for r in records if r.status in TERMINAL_STATUSES]
            elif status:
                try:
                    parsed = ToolCallStatus(status)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=f"unknown status filter: {status}") from e
                records = [r for r in records if r.status == parsed]
            if since is not None:
                records = [r for r in records if _event_cursor_for_record(self._events, r.tool_call_id) > since]
            return ToolCallListResponse(tool_calls=records[:limit], next_cursor=str(self._next_event_id - 1))

    def pending_approvals(self) -> PendingApprovalsResponse:
        with self._lock:
            approvals = [
                PendingApproval(
                    tool_call_id=r.tool_call_id,
                    server_id=r.server_id,
                    title=r.title or f"{r.server_title}: {r.tool_name}",
                    server_title=r.server_title,
                    tool_name=r.tool_name,
                    caller_principal=r.caller_principal,
                    rationale=r.rationale,
                    arguments=r.arguments,
                    created_at=r.created_at,
                )
                for r in self._tool_calls.values()
                if r.status == ToolCallStatus.PENDING_APPROVAL
            ]
            return PendingApprovalsResponse(approvals=sorted(approvals, key=lambda a: a.created_at))

    def events_since(self, since: int = 0) -> ToolCallEventsResponse:
        with self._lock:
            return ToolCallEventsResponse(events=[e for e in self._events if e.event_id > since])

    def mark_running(self, tool_call_id: str) -> tuple[ToolCallRecord, ToolCallEvent]:
        with self._lock:
            record = self.get(tool_call_id)
            if record.status != ToolCallStatus.PENDING_APPROVAL:
                raise HTTPException(
                    status_code=409, detail=f"tool call is not pending approval; status={record.status}"
                )
            updated = record.model_copy(
                update={"status": ToolCallStatus.RUNNING, "updated_at": dt.datetime.now(dt.UTC)}
            )
            self._tool_calls[record.tool_call_id] = updated
            event = self._append_event("tool_call_updated", updated)
            self._save()
            return updated, event

    def deny(self, tool_call_id: str, reason: str | None) -> tuple[ToolCallRecord, ToolCallEvent]:
        with self._lock:
            record = self.get(tool_call_id)
            if record.status != ToolCallStatus.PENDING_APPROVAL:
                raise HTTPException(
                    status_code=409, detail=f"tool call is not pending approval; status={record.status}"
                )
            updated = record.model_copy(
                update={
                    "status": ToolCallStatus.DENIED,
                    "updated_at": dt.datetime.now(dt.UTC),
                    "decision_reason": reason,
                }
            )
            self._tool_calls[record.tool_call_id] = updated
            event = self._append_event("tool_call_updated", updated)
            self._save()
            return updated, event

    def finish(
        self, tool_call_id: str, *, result: dict[str, Any] | None, error: str | None
    ) -> tuple[ToolCallRecord, ToolCallEvent]:
        with self._lock:
            record = self.get(tool_call_id)
            status = ToolCallStatus.OK if error is None else ToolCallStatus.ERROR
            updated = record.model_copy(
                update={"status": status, "updated_at": dt.datetime.now(dt.UTC), "result": result, "error": error}
            )
            self._tool_calls[tool_call_id] = updated
            event = self._append_event("tool_call_updated", updated)
            self._save()
            return updated, event


class PostgresToolCallStore:
    """Postgres-backed approval ledger for the deployed console."""

    def __init__(self, database_url: str) -> None:
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
        with self._engine.begin() as conn:
            run_migrations_for_connection(conn)

    def submit(
        self, *, server: McpServerEntry, req: SubmitToolCallRequest, caller_principal: str
    ) -> tuple[ToolCallRecord, list[ToolCallEvent], bool]:
        digest = _request_digest(req)
        try:
            return self._submit(server=server, req=req, caller_principal=caller_principal, digest=digest)
        except IntegrityError as e:
            if req.client_request_id is None:
                raise
            with self._engine.begin() as conn:
                record = self._record_by_client_request_id(conn, caller_principal, req.client_request_id)
            if record.request_digest != digest:
                raise HTTPException(
                    status_code=409, detail="client_request_id was already used for a different tool-call payload"
                ) from e
            return record, [], False

    def _submit(
        self, *, server: McpServerEntry, req: SubmitToolCallRequest, caller_principal: str, digest: str
    ) -> tuple[ToolCallRecord, list[ToolCallEvent], bool]:
        with self._engine.begin() as conn:
            if req.client_request_id is not None:
                existing = self._find_by_client_request_id(conn, caller_principal, req.client_request_id)
                if existing is not None:
                    if existing.request_digest != digest:
                        raise HTTPException(
                            status_code=409,
                            detail="client_request_id was already used for a different tool-call payload",
                        )
                    return existing, [], False

            now = dt.datetime.now(dt.UTC)
            record = ToolCallRecord(
                tool_call_id=f"tc_{secrets.token_hex(12)}",
                server_id=server.id,
                server_title=server.id,
                tool_name=req.tool_name,
                caller_principal=caller_principal,
                status=ToolCallStatus.PENDING_APPROVAL,
                created_at=now,
                updated_at=now,
                arguments=req.arguments,
                rationale=req.rationale,
                title=req.title,
                client_request_id=req.client_request_id,
                state_request_id=req.state_request_id,
                request_digest=digest,
            )
            self._upsert_record(conn, record)
            events = [
                self._insert_event(conn, "tool_call_submitted", record),
                self._insert_event(conn, "approval_pending", record),
            ]
            return record, events, True

    def get(self, tool_call_id: str) -> ToolCallRecord:
        with self._engine.begin() as conn:
            row = conn.execute(select(tool_calls).where(tool_calls.c.tool_call_id == tool_call_id)).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="tool call not found")
        return _record_from_row(row)

    def find_by_client_request_id(self, caller_principal: str, client_request_id: str) -> ToolCallRecord:
        with self._engine.begin() as conn:
            return self._record_by_client_request_id(conn, caller_principal, client_request_id)

    def list(self, *, status: str | None = None, since: int | None = None, limit: int = 100) -> ToolCallListResponse:
        if status is not None and status != "terminal":
            try:
                ToolCallStatus(status)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"unknown status filter: {status}") from e
        with self._engine.begin() as conn:
            last_event = (
                select(func.max(tool_call_events.c.event_id))
                .where(tool_call_events.c.tool_call_id == tool_calls.c.tool_call_id)
                .scalar_subquery()
            )
            stmt = select(tool_calls)
            if since is not None:
                stmt = stmt.where(func.coalesce(last_event, 0) > since)
            if status == "terminal":
                stmt = stmt.where(tool_calls.c.status.in_([s.value for s in TERMINAL_STATUSES]))
            elif status is not None:
                stmt = stmt.where(tool_calls.c.status == status)
            rows = conn.execute(stmt.order_by(tool_calls.c.created_at).limit(limit)).mappings().all()
            next_cursor = self._next_cursor(conn)
        records = [_record_from_row(row) for row in rows]
        return ToolCallListResponse(tool_calls=records, next_cursor=next_cursor)

    def pending_approvals(self) -> PendingApprovalsResponse:
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    select(tool_calls)
                    .where(tool_calls.c.status == ToolCallStatus.PENDING_APPROVAL.value)
                    .order_by(tool_calls.c.created_at)
                )
                .mappings()
                .all()
            )
        records = [_record_from_row(row) for row in rows]
        approvals = [
            PendingApproval(
                tool_call_id=r.tool_call_id,
                server_id=r.server_id,
                title=r.title or f"{r.server_title}: {r.tool_name}",
                server_title=r.server_title,
                tool_name=r.tool_name,
                caller_principal=r.caller_principal,
                rationale=r.rationale,
                arguments=r.arguments,
                created_at=r.created_at,
            )
            for r in records
        ]
        return PendingApprovalsResponse(approvals=approvals)

    def events_since(self, since: int = 0) -> ToolCallEventsResponse:
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    select(tool_call_events)
                    .where(tool_call_events.c.event_id > since)
                    .order_by(tool_call_events.c.event_id)
                )
                .mappings()
                .all()
            )
        return ToolCallEventsResponse(events=[_event_from_db(row) for row in rows])

    def mark_running(self, tool_call_id: str) -> tuple[ToolCallRecord, ToolCallEvent]:
        with self._engine.begin() as conn:
            record = self._record_by_tool_call_id(conn, tool_call_id)
            if record.status != ToolCallStatus.PENDING_APPROVAL:
                raise HTTPException(
                    status_code=409, detail=f"tool call is not pending approval; status={record.status}"
                )
            updated = record.model_copy(
                update={"status": ToolCallStatus.RUNNING, "updated_at": dt.datetime.now(dt.UTC)}
            )
            self._upsert_record(conn, updated)
            event = self._insert_event(conn, "tool_call_updated", updated)
            return updated, event

    def deny(self, tool_call_id: str, reason: str | None) -> tuple[ToolCallRecord, ToolCallEvent]:
        with self._engine.begin() as conn:
            record = self._record_by_tool_call_id(conn, tool_call_id)
            if record.status != ToolCallStatus.PENDING_APPROVAL:
                raise HTTPException(
                    status_code=409, detail=f"tool call is not pending approval; status={record.status}"
                )
            updated = record.model_copy(
                update={
                    "status": ToolCallStatus.DENIED,
                    "updated_at": dt.datetime.now(dt.UTC),
                    "decision_reason": reason,
                }
            )
            self._upsert_record(conn, updated)
            event = self._insert_event(conn, "tool_call_updated", updated)
            return updated, event

    def finish(
        self, tool_call_id: str, *, result: dict[str, Any] | None, error: str | None
    ) -> tuple[ToolCallRecord, ToolCallEvent]:
        with self._engine.begin() as conn:
            record = self._record_by_tool_call_id(conn, tool_call_id)
            status = ToolCallStatus.OK if error is None else ToolCallStatus.ERROR
            updated = record.model_copy(
                update={"status": status, "updated_at": dt.datetime.now(dt.UTC), "result": result, "error": error}
            )
            self._upsert_record(conn, updated)
            event = self._insert_event(conn, "tool_call_updated", updated)
            return updated, event

    def _upsert_record(self, conn: Any, record: ToolCallRecord) -> None:
        values = _record_values(record)
        update_values = {k: v for k, v in values.items() if k != "tool_call_id"}
        conn.execute(update(tool_calls).where(tool_calls.c.tool_call_id == record.tool_call_id).values(**update_values))
        if conn.execute(
            select(tool_calls.c.tool_call_id).where(tool_calls.c.tool_call_id == record.tool_call_id)
        ).first():
            return
        conn.execute(insert(tool_calls).values(**values))

    def _insert_event(
        self,
        conn: Any,
        event_type: Literal["tool_call_submitted", "approval_pending", "tool_call_updated"],
        record: ToolCallRecord,
    ) -> ToolCallEvent:
        created_at = dt.datetime.now(dt.UTC)
        row = (
            conn.execute(
                insert(tool_call_events)
                .values(
                    event_type=event_type,
                    tool_call_id=record.tool_call_id,
                    status=record.status.value,
                    created_at=created_at,
                )
                .returning(tool_call_events.c.event_id)
            )
            .mappings()
            .one()
        )
        return ToolCallEvent(
            event_id=int(row["event_id"]),
            event_type=event_type,
            tool_call_id=record.tool_call_id,
            status=record.status,
            created_at=created_at,
        )

    def _record_by_tool_call_id(self, conn: Any, tool_call_id: str) -> ToolCallRecord:
        row = (
            conn.execute(select(tool_calls).where(tool_calls.c.tool_call_id == tool_call_id).with_for_update())
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="tool call not found")
        return _record_from_row(row)

    def _find_by_client_request_id(
        self, conn: Any, caller_principal: str, client_request_id: str
    ) -> ToolCallRecord | None:
        row = (
            conn.execute(
                select(tool_calls).where(
                    tool_calls.c.caller_principal == caller_principal,
                    tool_calls.c.client_request_id == client_request_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return _record_from_row(row)

    def _record_by_client_request_id(self, conn: Any, caller_principal: str, client_request_id: str) -> ToolCallRecord:
        record = self._find_by_client_request_id(conn, caller_principal, client_request_id)
        if record is None:
            raise HTTPException(status_code=404, detail="tool call not found")
        return record

    def _next_cursor(self, conn: Any) -> str:
        row = (
            conn.execute(select(func.coalesce(func.max(tool_call_events.c.event_id), 0).label("event_id")))
            .mappings()
            .one()
        )
        return str(row["event_id"])


class ToolCallEventHub:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, events: Iterable[ToolCallEvent]) -> None:
        payloads = [e.model_dump(mode="json") for e in events]
        if not payloads or not self._connections:
            return
        dead: list[WebSocket] = []
        for ws in self._connections:
            try:
                for payload in payloads:
                    await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


class McpToolExecutor:
    async def execute(self, server: McpServerEntry, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with Client(server.server_url, auth=_credential_token(server)) as client:
            result = await client.call_tool(tool_name, arguments)
        return _mcp_result_to_json(result)


class McpMetadataProvider:
    async def metadata(self, server: McpServerEntry) -> ServerMetadata:
        try:
            async with Client(server.server_url, auth=_credential_token(server)) as client:
                tools = await client.list_tools()
        except Exception as e:
            return ServerMetadata(
                server_id=server.id, title=server.id, tools=[], schema_source="unavailable", degraded_reason=str(e)
            )
        reflected: list[ToolMetadata] = []
        for tool in tools:
            schema = tool.inputSchema
            if not isinstance(schema, dict):
                schema = {}
            reflected.append(
                ToolMetadata(name=tool.name, description=tool.description, input_schema=schema, schema_source="mcp")
            )
        return ServerMetadata(server_id=server.id, title=server.id, tools=reflected, schema_source="mcp")


def make_store(settings: Settings) -> ToolCallStoreProtocol:
    if settings.mcp_approval_database_url is not None:
        return PostgresToolCallStore(settings.mcp_approval_database_url.get_secret_value())
    return ToolCallStore(settings.mcp_approval_store_path)


def make_event_hub() -> ToolCallEventHub:
    return ToolCallEventHub()


def make_executor() -> McpToolExecutor:
    return McpToolExecutor()


def make_metadata_provider() -> McpMetadataProvider:
    return McpMetadataProvider()


def _store(request: Request) -> ToolCallStoreProtocol:
    return cast(ToolCallStoreProtocol, request.app.state.tool_call_store)


def _hub(request: Request) -> ToolCallEventHub:
    return cast(ToolCallEventHub, request.app.state.tool_call_event_hub)


def _executor(request: Request) -> McpToolExecutor:
    return cast(McpToolExecutor, request.app.state.tool_call_executor)


def _metadata_provider(request: Request) -> McpMetadataProvider:
    return cast(McpMetadataProvider, request.app.state.tool_call_metadata_provider)


StoreDep = Annotated[ToolCallStoreProtocol, Depends(_store)]
HubDep = Annotated[ToolCallEventHub, Depends(_hub)]
ExecutorDep = Annotated[McpToolExecutor, Depends(_executor)]
MetadataProviderDep = Annotated[McpMetadataProvider, Depends(_metadata_provider)]


def _request_digest(req: SubmitToolCallRequest) -> str:
    payload = {
        "server_id": req.server_id,
        "tool_name": req.tool_name,
        "arguments": req.arguments,
        "rationale": req.rationale,
        "title": req.title,
        "state_request_id": req.state_request_id,
    }
    return hashlib.sha256(canonicaljson.encode_canonical_json(payload)).hexdigest()


def _record_values(record: ToolCallRecord) -> dict[str, Any]:
    return ToolCallDbRow.from_record(record).model_dump(mode="python", by_alias=True)


def _record_from_row(row: Any) -> ToolCallRecord:
    return ToolCallDbRow.model_validate(dict(row)).to_record()


def _event_from_db(row: Any) -> ToolCallEvent:
    return ToolCallEventDbRow.model_validate(dict(row)).to_event()


def _event_cursor_for_record(events: list[ToolCallEvent], tool_call_id: str) -> int:
    matching = [e.event_id for e in events if e.tool_call_id == tool_call_id]
    return max(matching) if matching else 0


def _mcp_result_to_json(result: Any) -> dict[str, Any]:
    if hasattr(result, "structured_content") and result.structured_content is not None:
        return {"structured_content": result.structured_content}
    if hasattr(result, "model_dump"):
        return cast(dict[str, Any], result.model_dump(mode="json"))
    return {"repr": repr(result)}


def _credential_env_name(bearer_token_secret: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", bearer_token_secret).strip("_").upper()
    return f"HAKU_CONSOLE_MCP_CREDENTIAL_{suffix}"


def _credential_token(server: McpServerEntry) -> str | None:
    if server.bearer_token_secret is None:
        return None
    env_name = _credential_env_name(server.bearer_token_secret)
    token = os.environ.get(env_name)
    if not token:
        raise RuntimeError(f"missing MCP bearer token env var {env_name} for MCP server {server.id}")
    return token


def _load_servers(settings: Settings) -> list[McpServerEntry]:
    path = settings.mcp_approval_catalog_path
    if path is None or not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return McpServerCatalogFile.model_validate(raw).servers


def _server_entry(settings: Settings, server_id: str) -> McpServerEntry:
    for server in _load_servers(settings):
        if server.id == server_id:
            return server
    raise HTTPException(status_code=404, detail=f"unknown MCP server: {server_id}")


def _caller_principal(request: Request, settings: Settings) -> str:
    auth = request.headers.get("authorization", "")
    operator = request.headers.get("x-authentik-username")
    expected = settings.agent_api_token.get_secret_value() if settings.agent_api_token else None
    if expected and auth == f"Bearer {expected}":
        return "haku-agent-api-token"
    if operator:
        return operator
    if expected and request.method == "POST" and request.url.path == "/api/tool-calls":
        raise HTTPException(status_code=401, detail="missing or invalid tool-call API token")
    return "operator"


async def _maybe_execute(
    record: ToolCallRecord,
    server: McpServerEntry,
    store: ToolCallStoreProtocol,
    hub: ToolCallEventHub,
    executor: McpToolExecutor,
) -> ToolCallRecord:
    if record.status != ToolCallStatus.RUNNING:
        return record
    try:
        result = await executor.execute(server, record.tool_name, record.arguments)
    except Exception as e:
        updated, event = store.finish(record.tool_call_id, result=None, error=str(e))
    else:
        updated, event = store.finish(record.tool_call_id, result=result, error=None)
    await hub.broadcast([event])
    logger.info(
        "tool call %s finished status=%s server=%s tool=%s digest=%s",
        updated.tool_call_id,
        updated.status,
        updated.server_id,
        updated.tool_name,
        updated.request_digest,
    )
    return updated


async def _wait_terminal(store: ToolCallStoreProtocol, tool_call_id: str, wait_for_ms: int) -> ToolCallRecord:
    deadline = asyncio.get_running_loop().time() + (wait_for_ms / 1000)
    while True:
        record = store.get(tool_call_id)
        if record.status in TERMINAL_STATUSES or wait_for_ms <= 0:
            return record
        if asyncio.get_running_loop().time() >= deadline:
            return record
        await asyncio.sleep(0.05)


@router.get("/api/capabilities/mcp-servers")
async def mcp_servers(settings: SettingsDep, metadata_provider: MetadataProviderDep) -> ToolCapabilitiesResponse:
    return ToolCapabilitiesResponse(
        servers=[await metadata_provider.metadata(server) for server in _load_servers(settings)]
    )


@router.post("/api/tool-calls")
async def submit_tool_call(
    body: SubmitToolCallRequest,
    request: Request,
    settings: SettingsDep,
    store: StoreDep,
    hub: HubDep,
    executor: ExecutorDep,
) -> ToolCallRecord:
    server = _server_entry(settings, body.server_id)
    caller = _caller_principal(request, settings)
    record, events, created = store.submit(server=server, req=body, caller_principal=caller)
    await hub.broadcast(events)
    if created:
        logger.info(
            "tool call %s submitted status=%s server=%s tool=%s caller=%s digest=%s",
            record.tool_call_id,
            record.status,
            record.server_id,
            record.tool_name,
            caller,
            record.request_digest,
        )
    if record.status == ToolCallStatus.RUNNING:
        record = await _maybe_execute(record, server, store, hub, executor)
    return await _wait_terminal(store, record.tool_call_id, body.wait_for_ms)


@router.get("/api/tool-calls")
async def list_tool_calls(
    store: StoreDep,
    status: str | None = None,
    since: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ToolCallListResponse:
    return store.list(status=status, since=since, limit=limit)


@router.get("/api/tool-calls/by-client-request/{client_request_id:path}")
async def get_tool_call_by_client_request(
    client_request_id: str, request: Request, settings: SettingsDep, store: StoreDep
) -> ToolCallRecord:
    return store.find_by_client_request_id(_caller_principal(request, settings), client_request_id)


@router.get("/api/tool-calls/{tool_call_id}")
async def get_tool_call(tool_call_id: str, store: StoreDep) -> ToolCallRecord:
    return store.get(tool_call_id)


@router.get("/api/approvals/pending")
async def pending_approvals(store: StoreDep) -> PendingApprovalsResponse:
    return store.pending_approvals()


@router.get("/api/approvals/events")
async def approval_events(store: StoreDep, since: int = 0) -> ToolCallEventsResponse:
    return store.events_since(since)


@router.post("/api/tool-calls/{tool_call_id}/decision")
async def decide_approval(
    tool_call_id: str,
    body: ApprovalDecisionRequest,
    request: Request,
    csrf_protect: Csrf,
    settings: SettingsDep,
    store: StoreDep,
    hub: HubDep,
    executor: ExecutorDep,
) -> ApprovalDecisionResponse:
    await csrf_protect.validate_csrf(request)
    if body.decision == "deny":
        record, event = store.deny(tool_call_id, body.reason)
        await hub.broadcast([event])
        logger.info(
            "tool call %s denied server=%s tool=%s reason=%r",
            record.tool_call_id,
            record.server_id,
            record.tool_name,
            body.reason,
        )
        return ApprovalDecisionResponse(tool_call=record)
    running, running_event = store.mark_running(tool_call_id)
    await hub.broadcast([running_event])
    server = _server_entry(settings, running.server_id)
    finished = await _maybe_execute(running, server, store, hub, executor)
    return ApprovalDecisionResponse(tool_call=finished)


@router.websocket("/api/approvals/ws")
async def approvals_ws(websocket: WebSocket) -> None:
    hub = websocket.app.state.tool_call_event_hub
    await hub.connect(websocket)
    try:
        await websocket.send_json({"type": "hello"})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(websocket)
