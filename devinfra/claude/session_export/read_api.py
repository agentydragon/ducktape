"""Read-only Claude Code-shaped routes backed by the synchronized PostgreSQL mirror."""

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette import status

from devinfra.claude.session_export.models import Event, SessionSummary, canonical_id
from devinfra.claude.session_export.store import SessionStore


class SessionListPage(BaseModel):
    data: list[SessionSummary]
    next_cursor: str | None
    resume_token: str | None = None


class SessionDetail(BaseModel):
    session: SessionSummary


class SessionEventPage(BaseModel):
    data: list[Event]
    has_more: bool
    first_id: str | None
    last_id: str | None


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _encode_session_cursor(timestamp: datetime, session_id: str) -> str:
    raw = json.dumps([timestamp.isoformat(), session_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_session_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        timestamp, session_id = json.loads(raw)
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None or not isinstance(session_id, str):
            raise ValueError
        return parsed, session_id
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as invalid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid session cursor.") from invalid


def _encode_resume_token(revision: int) -> str:
    return f"1:{revision}"


def _decode_resume_token(token: str | None) -> int:
    try:
        version, value = (token or "").split(":", 1)
        if version != "1" or not value.isdecimal():
            raise ValueError
        return int(value)
    except ValueError as invalid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid session resume token.") from invalid


def _sse_event(revision: int, event: str, data: object) -> str:
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {_encode_resume_token(revision)}\nevent: {event}\ndata: {body}\n\n"


def _session_id(value: str) -> str:
    if not value.startswith(("session_", "cse_")):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found.")
    return canonical_id(value)


async def _event_cursor(store: SessionStore, session_id: str, cursor: str | None) -> int | None:
    if cursor is None:
        return None
    if cursor.isdecimal():
        return int(cursor)
    try:
        event_id = UUID(cursor)
    except ValueError as invalid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid event cursor.") from invalid
    sequence = await store.sequence_num_of(session_id, event_id)
    if sequence is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown event cursor.")
    return sequence


def create_read_api(store: SessionStore) -> APIRouter:
    """Claude Code-compatible list, detail, and event history route shapes, restricted to reads."""
    router = APIRouter(prefix="/v1/code", dependencies=[Depends(_no_store)])

    @router.get("/sessions", response_model=SessionListPage)
    async def list_sessions(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        cursor: str | None = None,
        statuses: Annotated[list[str] | None, Query()] = None,
    ) -> SessionListPage:
        # Claude Code's initial unfiltered bootstrap asks for active and paused sessions.
        effective_statuses = statuses if statuses is not None else ["active", "paused"]
        before = _decode_session_cursor(cursor)
        # Take the watermark before paging. A concurrent change may be replayed twice, never
        # omitted, because any write after this read has a greater revision.
        resume_token = _encode_resume_token(await store.current_change_revision())
        data, has_more, next_position = await store.session_page(
            limit=limit, statuses=effective_statuses, before=before
        )
        next_cursor = _encode_session_cursor(*next_position) if has_more and next_position is not None else None
        return SessionListPage(data=data, next_cursor=next_cursor, resume_token=resume_token)

    @router.get("/sessions/watch")
    async def watch_sessions(
        request: Request, resume_token: str | None = None, last_event_id: Annotated[str | None, Header()] = None
    ) -> StreamingResponse:
        token = last_event_id or resume_token
        if token is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Load a session page before opening the watch.")
        start_revision = _decode_resume_token(token)

        async def events() -> AsyncIterator[str]:
            revision = start_revision
            # Each web replica listens independently. PostgreSQL notifications only wake this loop;
            # the durable journal is the replay source after disconnects and reconnects.
            async with store.listen_for_changes() as notified:
                while not await request.is_disconnected():
                    notified.clear()
                    changes = await store.changes_after(revision)
                    if revision < changes.oldest_retained or revision > changes.current_revision:
                        revision = changes.current_revision
                        yield _sse_event(revision, "reset", {"reason": "resume_cursor_unavailable"})
                        continue
                    if changes.changes:
                        for change in changes.changes:
                            revision = change.revision
                            yield _sse_event(revision, "changed", {"session_ids": change.session_ids})
                        continue
                    try:
                        await asyncio.wait_for(notified.wait(), timeout=15)
                    except TimeoutError:
                        yield ": keep-alive\n\n"

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        )

    @router.get("/sessions/{session_id}", response_model=SessionDetail)
    async def get_session(session_id: str) -> SessionDetail:
        raw = await store.session(_session_id(session_id))
        if raw is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found.")
        return SessionDetail(session=raw)

    @router.get("/sessions/{session_id}/events", response_model=SessionEventPage)
    async def list_events(
        session_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 500,
        sort_order: Literal["asc", "desc"] = "asc",
        cursor: str | None = None,
    ) -> SessionEventPage:
        canonical = _session_id(session_id)
        if await store.session(canonical) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found.")
        sequence = await _event_cursor(store, canonical, cursor)
        data, has_more = await store.event_page(
            canonical,
            limit=limit,
            sort_order=sort_order,
            after=sequence if sort_order == "asc" else None,
            before=sequence if sort_order == "desc" else None,
        )
        events = data
        return SessionEventPage(
            data=events,
            has_more=has_more,
            first_id=events[0].event_id if events else None,
            last_id=events[-1].event_id if events else None,
        )

    return router
