"""PostgreSQL browser sessions; only a random, signed handle crosses into the browser.

A row's JSON payload is Starlette's session -- authlib's state/nonce/PKCE data while a login is
pending, and the consent flow's interactions -- and the operator's login is typed columns beside it.
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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from itsdangerous import BadSignature, TimestampSigner
from pydantic import SecretStr
from sqlalchemy import CheckConstraint, DateTime, Text, delete, select
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

# How long a login may take from /auth/login to its callback.
_PENDING_LOGIN = timedelta(minutes=10)


@dataclass(frozen=True)
class LoginTokens:
    """The login's access token and what renews it, kept only while Action federation needs them."""

    access_token: SecretStr
    # The access token's own expiry, not the session's.
    expires_at: datetime
    # None when the provider issued none.
    refresh_token: SecretStr | None = None


@dataclass(frozen=True)
class OperatorSession:
    issuer: str
    subject: str
    username: str
    tokens: LoginTokens | None = None


class BrowserSession(Base):
    __tablename__ = "operator_browser_session"
    __table_args__ = (
        CheckConstraint(
            "(operator_issuer IS NULL) = (operator_subject IS NULL) "
            "AND (operator_issuer IS NULL) = (operator_username IS NULL)",
            name="operator_browser_session_operator_whole",
        ),
        CheckConstraint(
            "(access_token IS NULL) = (access_token_expires_at IS NULL)",
            name="operator_browser_session_access_token_whole",
        ),
        CheckConstraint(
            "access_token IS NULL OR operator_issuer IS NOT NULL", name="operator_browser_session_tokens_logged_in"
        ),
        CheckConstraint(
            "refresh_token IS NULL OR access_token IS NOT NULL",
            name="operator_browser_session_refresh_token_with_access_token",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # The login, all NULL while it is pending.
    operator_issuer: Mapped[str | None] = mapped_column(Text)
    operator_subject: Mapped[str | None] = mapped_column(Text)
    operator_username: Mapped[str | None] = mapped_column(Text)
    access_token: Mapped[str | None] = mapped_column(Text)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_token: Mapped[str | None] = mapped_column(Text)

    @property
    def login(self) -> OperatorSession | None:
        """The login this row holds, or None while it is pending."""
        if self.operator_issuer is None:
            return None
        # The check constraints keep the identity whole, and the access token with its expiry.
        assert self.operator_subject is not None
        assert self.operator_username is not None
        tokens = None
        if self.access_token is not None:
            assert self.access_token_expires_at is not None
            tokens = LoginTokens(
                access_token=SecretStr(self.access_token),
                expires_at=self.access_token_expires_at,
                refresh_token=SecretStr(self.refresh_token) if self.refresh_token is not None else None,
            )
        return OperatorSession(
            issuer=self.operator_issuer, subject=self.operator_subject, username=self.operator_username, tokens=tokens
        )

    @login.setter
    def login(self, login: OperatorSession) -> None:
        self.operator_issuer = login.issuer
        self.operator_subject = login.subject
        self.operator_username = login.username
        tokens = login.tokens
        self.access_token = tokens.access_token.get_secret_value() if tokens is not None else None
        self.access_token_expires_at = tokens.expires_at if tokens is not None else None
        self.refresh_token = (
            tokens.refresh_token.get_secret_value() if tokens is not None and tokens.refresh_token is not None else None
        )

    def record_activity(self, idle: timedelta, step: timedelta, now: datetime) -> None:
        """Move the idle deadline, once it would move by more than `step` (or half the idle timeout)."""
        deadline = min(self.absolute_expires_at, now + idle)
        if deadline - self.expires_at > min(step, idle / 2):
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


@dataclass
class HeldLogin:
    """A session's login, None once the session has expired or ended (or while its login is pending).
    Whoever holds it may replace the login to save renewed tokens, or clear it to end the session."""

    login: OperatorSession | None


class SessionRow:
    """A request's session row, for reading or changing it as it is now rather than as the request's
    snapshot, which another request or replica may since have changed.

    Until the response headers are out the middleware holds the row lock, which a second transaction
    would wait on forever; `locked()` then hands out the request's own login, which the middleware
    saves. Afterwards -- in a streamed body -- it takes the lock in a transaction of its own.
    """

    def __init__(
        self,
        store: OperatorSessionStore,
        row_id: str,
        *,
        idle: timedelta,
        step: timedelta,
        request_login: HeldLogin | None,
    ) -> None:
        self.id = row_id
        self._store = store
        self._idle = idle
        self._step = step
        self._request_login = request_login
        self._mutex = asyncio.Lock()

    @asynccontextmanager
    async def locked(self) -> AsyncIterator[HeldLogin]:
        """The login as the row holds it now, held exclusively. Counts as activity."""
        async with self._mutex:
            if self._request_login is not None:
                yield self._request_login
                return
            async with self._store.sessions.begin() as db:
                row = await db.scalar(select(BrowserSession).where(BrowserSession.id == self.id).with_for_update())
                now = datetime.now(UTC)
                if row is not None and row.expires_at <= now:
                    await _end(db, row)
                    row = None
                if row is None:
                    yield HeldLogin(None)
                    return
                row.record_activity(self._idle, self._step, now)
                login = row.login
                held = HeldLogin(login)
                yield held
                if held.login != login:
                    if held.login is None:
                        await _end(db, row)
                    else:
                        row.login = held.login

    async def release(self) -> None:
        """For the middleware, as it takes the response headers: it is about to save and unlock."""
        async with self._mutex:
            self._request_login = None


@dataclass(frozen=True)
class NewLogin:
    login: OperatorSession
    absolute_expires_at: datetime


class RequestSession:
    """A request's browser session as the middleware read it, and what the request makes of it, which
    the middleware saves before the response headers go out. `request.session` is its payload.

    A session whose login ends is over, whatever its payload still holds; one with neither a login
    nor a payload is no session at all.
    """

    def __init__(self, payload: dict[str, Any], held: HeldLogin, row: SessionRow | None) -> None:
        self._payload = payload
        self.held = held
        self.row = row
        self.new_login: NewLogin | None = None

    def log_in(self, login: OperatorSession, absolute_expires_at: datetime) -> None:
        """The callback's: `login` replaces the session, in a new row under a new handle, so that the
        handle the pending login was reached by cannot reach it. The pending login's state is spent."""
        self._payload.clear()
        self.new_login = NewLogin(login, absolute_expires_at)

    def end(self) -> None:
        self._payload.clear()
        self.held.login = None
        self.new_login = None


def request_session(request: Request) -> RequestSession:
    """The browser session the middleware read for this request; only an app with a login has one."""
    session = request.state.request_session
    if not isinstance(session, RequestSession):
        raise TypeError(f"request.state.request_session is {type(session).__name__}, not RequestSession")
    return session


def operator_session_row(request: Request) -> SessionRow:
    """The row this request's operator session was read from; only a request that found one may ask."""
    row = request_session(request).row
    if row is None:
        raise TypeError("this request's operator session has no row")
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
        activity_step_seconds: int,
    ) -> None:
        super().__init__(app)
        self._store = store
        self._signer = TimestampSigner(secret_key, salt="agentplane-operator-session-v1")
        self._cookie = session_cookie
        self._secure = https_only
        self._max_age = max_age
        self._idle = timedelta(seconds=idle_seconds)
        self._step = timedelta(seconds=activity_step_seconds)

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
                    row.record_activity(self._idle, self._step, now)
            initial = copy.deepcopy(row.payload) if row is not None else {}
            initial_login = row.login if row is not None else None
            request.scope["session"] = copy.deepcopy(initial)
            held = HeldLogin(initial_login)
            current = (
                SessionRow(self._store, row.id, idle=self._idle, step=self._step, request_login=held)
                if row is not None
                else None
            )
            session = RequestSession(request.scope["session"], held, current)
            request.state.request_session = session
            response = await call_next(request)
            if current is not None:
                await current.release()
            payload = request.session
            new_login = session.new_login
            login = new_login.login if new_login is not None else held.login
            kept = login is not None or (bool(payload) and initial_login is None)
            if payload != initial or login != initial_login or new_login is not None:
                if row is not None and (not kept or new_login is not None):
                    await db.delete(row)
                    row, ended = None, True
                if kept:
                    if row is None:
                        now = datetime.now(UTC)
                        absolute = (
                            new_login.absolute_expires_at
                            if new_login is not None
                            else now + min(_PENDING_LOGIN, timedelta(seconds=self._max_age))
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
                    if login is not None:
                        row.login = login
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
            if new_login is not None:
                await db.execute(delete(BrowserSession).where(BrowserSession.expires_at <= datetime.now(UTC)))
                ended = True
            if ended:
                # In the deleting transaction, so it is delivered with the commit that makes the deletion
                # visible and never for one rolled back: what ends a stream on the session, on any replica.
                await notify(db, Channel.OPERATOR_SESSIONS)
        response.headers["Cache-Control"] = "no-store"
        return response
