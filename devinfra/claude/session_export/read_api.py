"""Read-only Claude Code-shaped routes backed by the synchronized PostgreSQL mirror."""

import base64
import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
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
        data, has_more, next_position = await store.session_page(
            limit=limit, statuses=effective_statuses, before=before
        )
        next_cursor = _encode_session_cursor(*next_position) if has_more and next_position is not None else None
        return SessionListPage(
            data=[SessionSummary.model_validate(session) for session in data],
            next_cursor=next_cursor,
        )

    @router.get("/sessions/{session_id}", response_model=SessionDetail)
    async def get_session(session_id: str) -> SessionDetail:
        raw = await store.session(_session_id(session_id))
        if raw is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found.")
        return SessionDetail(session=SessionSummary.model_validate(raw))

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
        events = [Event.model_validate(event) for event in data]
        return SessionEventPage(
            data=events,
            has_more=has_more,
            first_id=events[0].event_id if events else None,
            last_id=events[-1].event_id if events else None,
        )

    return router
