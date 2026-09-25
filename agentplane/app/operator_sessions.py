"""PostgreSQL browser sessions; only a random, signed handle crosses into the browser.

Authlib's state/nonce/PKCE data and the operator's login tokens stay in the same server-side row.
A row lock serializes concurrent callbacks/logout across replicas. Commit before sending headers,
not after streaming the response, so logout cannot be undone by an older in-flight save.

A row expires at the earlier of its absolute deadline, fixed when it is created, and an idle
deadline that every request presenting it moves forward.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from itsdangerous import BadSignature, TimestampSigner
from sqlalchemy import DateTime, Text, delete, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from agentplane.app.changes import Changes
from agentplane.app.database import Base
from agentplane.app.database_updates import Channel, notify

# A request moves the idle deadline only once it would move by more than this, so a burst of
# requests does not each rewrite the row; the idle timeout therefore holds to within this much.
ACTIVITY_STEP = timedelta(minutes=5)

# How long a login may take from /auth/login to its callback.
_PENDING_LOGIN = timedelta(minutes=10)


class BrowserSession(Base):
    __tablename__ = "operator_browser_session"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)

    def record_activity(self, idle: timedelta, now: datetime) -> None:
        deadline = min(self.absolute_expires_at, now + idle)
        if deadline - self.expires_at > min(ACTIVITY_STEP, idle / 2):
            self.expires_at = deadline


class OperatorSessionStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def until_ended(self, session_id: str, changes: Changes) -> None:
        """Return once the session's row is deleted, on any replica, or its current `expires_at` has
        passed. Each wake from `changes` (the sessions channel) and each deadline re-reads the row."""
        changed = asyncio.Event()
        with changes.subscribe(changed):
            while True:
                changed.clear()
                async with self.sessions() as db:
                    expires_at = await db.scalar(
                        select(BrowserSession.expires_at).where(BrowserSession.id == session_id)
                    )
                if expires_at is None or expires_at <= datetime.now(UTC):
                    return
                with suppress(TimeoutError):
                    await asyncio.wait_for(changed.wait(), (expires_at - datetime.now(UTC)).total_seconds())


class SessionRow:
    """A request's session row, for reading or changing it as it is now rather than as the request's
    snapshot, which another request or replica may since have changed.

    Until the response headers are out the middleware holds the row lock, which a second transaction
    would wait on forever; `locked()` then hands out the request's own session, which the middleware
    saves. Afterwards -- in a streamed body -- it takes the lock in a transaction of its own.
    """

    def __init__(
        self, store: OperatorSessionStore, row_id: str, *, idle: timedelta, request_session: dict[str, Any] | None
    ) -> None:
        self.id = row_id
        self._store = store
        self._idle = idle
        self._request_session = request_session
        self._mutex = asyncio.Lock()

    @asynccontextmanager
    async def locked(self) -> AsyncIterator[dict[str, Any] | None]:
        """The payload, held exclusively, or None once the session has expired or ended. What the block
        leaves in the payload is saved, and leaving it empty ends the session. Counts as activity."""
        async with self._mutex:
            if self._request_session is not None:
                yield self._request_session
                return
            async with self._store.sessions.begin() as db:
                row = await db.scalar(select(BrowserSession).where(BrowserSession.id == self.id).with_for_update())
                now = datetime.now(UTC)
                if row is not None and row.expires_at <= now:
                    await _end(db, row)
                    row = None
                if row is None:
                    yield None
                    return
                row.record_activity(self._idle, now)
                payload = copy.deepcopy(row.payload)
                yield payload
                if not payload:
                    await _end(db, row)
                elif payload != row.payload:
                    row.payload = payload

    async def release(self) -> None:
        """For the middleware, as it takes the response headers: it is about to save and unlock."""
        async with self._mutex:
            self._request_session = None


def operator_session_row(request: Request) -> SessionRow:
    """The row this request's operator session was read from; only a request that found one may ask."""
    row = request.state.operator_session_row
    if not isinstance(row, SessionRow):
        raise TypeError(f"request.state.operator_session_row is {type(row).__name__}, not SessionRow")
    return row


async def _end(db: AsyncSession, row: BrowserSession) -> None:
    """Delete `row`, telling every replica's streams on it as the middleware's deletions do."""
    await db.delete(row)
    await notify(db, Channel.OPERATOR_SESSIONS)


class OperatorSessionMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        *,
        store: OperatorSessionStore,
        secret_key: str,
        session_cookie: str,
        https_only: bool,
        max_age: int,
        idle_seconds: int,
    ) -> None:
        super().__init__(app)
        self._store = store
        self._signer = TimestampSigner(secret_key, salt="agentplane-operator-session-v1")
        self._cookie = session_cookie
        self._secure = https_only
        self._max_age = max_age
        self._idle = timedelta(seconds=idle_seconds)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        handle: str | None = None
        cookie = request.cookies.get(self._cookie)
        if cookie is not None:
            with suppress(BadSignature, UnicodeError):
                handle = self._signer.unsign(cookie, max_age=self._max_age).decode("ascii")
        async with self._store.sessions.begin() as db:
            row = None
            ended = False
            if handle is not None:
                row = await db.scalar(
                    select(BrowserSession)
                    .where(BrowserSession.id == hashlib.sha256(handle.encode()).hexdigest())
                    .with_for_update()
                )
                now = datetime.now(UTC)
                if row is not None and row.expires_at <= now:
                    await db.delete(row)
                    row, ended = None, True
                if row is not None:
                    row.record_activity(self._idle, now)
            initial = copy.deepcopy(row.payload) if row is not None else {}
            request.scope["session"] = copy.deepcopy(initial)
            current = (
                SessionRow(self._store, row.id, idle=self._idle, request_session=request.scope["session"])
                if row is not None
                else None
            )
            request.state.operator_session_row = current
            request.state.rotate_operator_session = False
            request.state.operator_session_absolute_expires_at = None
            response = await call_next(request)
            if current is not None:
                await current.release()
            payload = request.session
            rotate = request.state.rotate_operator_session
            changed = payload != initial or rotate
            if changed:
                if row is not None and (not payload or rotate):
                    await db.delete(row)
                    row, ended = None, True
                if payload:
                    if row is None:
                        now = datetime.now(UTC)
                        absolute = request.state.operator_session_absolute_expires_at or now + min(
                            _PENDING_LOGIN, timedelta(seconds=self._max_age)
                        )
                        handle = secrets.token_urlsafe(32)
                        row = BrowserSession(
                            id=hashlib.sha256(handle.encode()).hexdigest(),
                            payload=payload,
                            absolute_expires_at=absolute,
                            expires_at=min(absolute, now + self._idle),
                        )
                        db.add(row)
                    else:
                        row.payload = payload
                    assert handle is not None
                    response.set_cookie(
                        self._cookie,
                        self._signer.sign(handle).decode("ascii"),
                        max_age=max(0, int((row.absolute_expires_at - datetime.now(UTC)).total_seconds())),
                        httponly=True,
                        secure=self._secure,
                        samesite="lax",
                        path="/",
                    )
                else:
                    response.delete_cookie(self._cookie, path="/", secure=self._secure, httponly=True, samesite="lax")
            elif cookie is not None and row is None:
                response.delete_cookie(self._cookie, path="/", secure=self._secure, httponly=True, samesite="lax")
            # Bounded by login activity, not a background scheduler. Expired credentials are never read.
            if rotate:
                await db.execute(delete(BrowserSession).where(BrowserSession.expires_at <= datetime.now(UTC)))
                ended = True
            if ended:
                # In the deleting transaction, so it is delivered with the commit that makes the deletion
                # visible and never for one rolled back: what ends a stream on the session, on any replica.
                await notify(db, Channel.OPERATOR_SESSIONS)
        response.headers["Cache-Control"] = "no-store"
        return response
