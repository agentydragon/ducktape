"""PostgreSQL browser sessions; only a random, signed handle crosses into the browser.

A row's JSON payload is Starlette's session -- authlib's state/nonce/PKCE data while a login is
pending, and the consent flow's interactions -- and the operator's login is typed columns beside it.

No request holds its row while its handler runs, so requests presenting one session run
concurrently. The middleware reads the row before the handler and, before the response headers go
out, writes what the handler changed in a transaction that locks the row and applies the change to
the row as it is then. What changes the row in between -- a renewed login token, a consent
interaction -- does the same through `SessionRow`. A write that finds the row gone does not bring it
back, so an in-flight request cannot undo a logout.

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
from sqlalchemy import CheckConstraint, ColumnElement, DateTime, Select, Text, delete, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
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

    def activity_deadline(self, idle: timedelta, step: timedelta, now: datetime) -> datetime | None:
        """Where activity at `now` moves the idle deadline, or None while that would move it by no
        more than `step` (or half the idle timeout)."""
        deadline = min(self.absolute_expires_at, now + idle)
        return deadline if deadline - self.expires_at > min(step, idle / 2) else None


def _row_id(handle: str) -> str:
    return hashlib.sha256(handle.encode()).hexdigest()


def _unheld(row_id: str, *criteria: ColumnElement[bool]) -> Select[tuple[str]]:
    """`row_id`, locking its row, if the row meets `criteria` and no other transaction holds it."""
    return select(BrowserSession.id).where(BrowserSession.id == row_id, *criteria).with_for_update(skip_locked=True)


async def _end(db: AsyncSession, row: BrowserSession) -> None:
    """Delete `row`. Whichever transaction deletes a row notifies, so the notification arrives with
    the commit that makes the deletion visible and never for one rolled back: what ends a stream on
    the session, on any replica."""
    await db.delete(row)
    await notify(db, Channel.OPERATOR_SESSIONS)


async def _locked(db: AsyncSession, row_id: str) -> BrowserSession | None:
    """`row_id`'s row, locked for the rest of `db`'s transaction; None once its session has ended or
    expired, deleting an expired one."""
    row = await db.scalar(select(BrowserSession).where(BrowserSession.id == row_id).with_for_update())
    if row is not None and row.expires_at <= datetime.now(UTC):
        await _end(db, row)
        return None
    return row


def _insert(
    db: AsyncSession,
    payload: dict[str, Any],
    login: OperatorSession | None,
    absolute_expires_at: datetime,
    idle: timedelta,
) -> str:
    """Add a new row, returning the new handle that reaches it."""
    handle = secrets.token_urlsafe(32)
    row = BrowserSession(
        id=_row_id(handle),
        payload=payload,
        absolute_expires_at=absolute_expires_at,
        expires_at=min(absolute_expires_at, datetime.now(UTC) + idle),
    )
    if login is not None:
        row.login = login
    db.add(row)
    return handle


@dataclass(frozen=True)
class NewLogin:
    login: OperatorSession
    absolute_expires_at: datetime


class OperatorSessionStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def read(self, row_id: str, *, idle: timedelta, step: timedelta) -> BrowserSession | None:
        """The row as it is now, or None once its session has ended or expired. Counts as activity.

        Waits on no lock: a row another transaction holds -- a renewal holds it across its call to
        the identity provider -- keeps its idle deadline for a later request to move, and when it
        has expired, its holder or a later request deletes it.
        """
        async with self.sessions.begin() as db:
            row = await db.get(BrowserSession, row_id)
            now = datetime.now(UTC)
            if row is not None and row.expires_at <= now:
                expired = (
                    delete(BrowserSession)
                    .where(BrowserSession.id.in_(_unheld(row_id, BrowserSession.expires_at <= now)))
                    .returning(BrowserSession.id)
                    .execution_options(synchronize_session=False)
                )
                if await db.scalar(expired) is not None:
                    await notify(db, Channel.OPERATOR_SESSIONS)
                return None
            if row is not None and (deadline := row.activity_deadline(idle, step, now)) is not None:
                await db.execute(
                    update(BrowserSession)
                    .where(BrowserSession.id.in_(_unheld(row_id, BrowserSession.expires_at < deadline)))
                    .values(expires_at=deadline)
                    .execution_options(synchronize_session=False)
                )
        return row

    async def create(self, payload: dict[str, Any], absolute_expires_at: datetime, *, idle: timedelta) -> str:
        """A new pending login's row, returning the handle that reaches it."""
        async with self.sessions.begin() as db:
            return _insert(db, payload, None, absolute_expires_at, idle)

    async def log_in(
        self, pending: str | None, payload: dict[str, Any], new: NewLogin, *, idle: timedelta
    ) -> str | None:
        """A new row holding `new`, in place of `pending`'s when the request found one, returning the
        handle that reaches it; None, creating nothing, once `pending`'s row is gone: another request
        replaced it first, or its session ended."""
        async with self.sessions.begin() as db:
            if pending is not None:
                replaced = delete(BrowserSession).where(BrowserSession.id == pending).returning(BrowserSession.id)
                if await db.scalar(replaced) is None:
                    return None
            handle = _insert(db, payload, new.login, new.absolute_expires_at, idle)
            # Bounded by login activity, not a background scheduler. Expired credentials are never read.
            await db.execute(delete(BrowserSession).where(BrowserSession.expires_at <= datetime.now(UTC)))
            await notify(db, Channel.OPERATOR_SESSIONS)
        return handle

    async def save(self, row_id: str, before: dict[str, Any], after: dict[str, Any]) -> bool:
        """Make a request's change to its session's payload, `before` to `after`, to the payload as the
        row holds it now, key by key, so that what another request changed meanwhile stands. False once
        the session is over: it ended, or it is a pending login with nothing left in its payload."""
        async with self.sessions.begin() as db:
            row = await _locked(db, row_id)
            if row is None:
                return False
            payload = {key: value for key, value in row.payload.items() if key in after or key not in before}
            payload.update((key, value) for key, value in after.items() if key not in before or before[key] != value)
            if row.login is None and not payload:
                await _end(db, row)
                return False
            row.payload = payload
        return True

    async def end(self, row_id: str) -> None:
        """Delete the row, whatever another request is making of it meanwhile."""
        async with self.sessions.begin() as db:
            ended = delete(BrowserSession).where(BrowserSession.id == row_id).returning(BrowserSession.id)
            if await db.scalar(ended) is not None:
                await notify(db, Channel.OPERATOR_SESSIONS)

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
class HeldSession:
    """A logged-in session as its row holds it now, held exclusively until the block holding it
    exits, which saves what the block made of it. `login` is None once the session has ended or
    expired; clearing it ends the session."""

    login: OperatorSession | None
    payload: dict[str, Any]


class SessionRow:
    """A request's session row, for reading or changing it as it is now rather than as the request's
    snapshot, which another request or replica may since have changed. Each use is a short
    transaction of its own."""

    def __init__(self, store: OperatorSessionStore, row_id: str, *, idle: timedelta, step: timedelta) -> None:
        self.id = row_id
        self._store = store
        self._idle = idle
        self._step = step

    async def login(self) -> OperatorSession | None:
        """The login as the row holds it now; None once the session has ended or expired. Counts as
        activity."""
        row = await self._store.read(self.id, idle=self._idle, step=self._step)
        return row.login if row is not None else None

    @asynccontextmanager
    async def locked(self) -> AsyncIterator[HeldSession]:
        """The session under the row lock until the block exits. Every other write to the row waits
        for the block, so nothing in it may itself need the lock, as an exchange renewing the login
        does."""
        async with self._store.sessions.begin() as db:
            row = await _locked(db, self.id)
            login = row.login if row is not None else None
            if row is None or login is None:
                yield HeldSession(None, {})
                return
            held = HeldSession(login, copy.deepcopy(row.payload))
            yield held
            if held.login is None:
                await _end(db, row)
                return
            if held.login != login:
                row.login = held.login
            if held.payload != row.payload:
                row.payload = held.payload


@dataclass(frozen=True)
class Logout:
    """The request ended its session."""


class RequestSession:
    """A request's browser session as the middleware read it, and what the request makes of it, which
    the middleware saves before the response headers go out. `request.session` is its payload, and
    `login` the login the request found, whatever the request or another makes of the row since.

    A session whose login ends is over, whatever its payload still holds; one with neither a login
    nor a payload is no session at all.
    """

    def __init__(self, payload: dict[str, Any], login: OperatorSession | None, row: SessionRow | None) -> None:
        self._payload = payload
        self.login = login
        self.row = row
        self.replaced: NewLogin | Logout | None = None

    def log_in(self, login: OperatorSession, absolute_expires_at: datetime) -> None:
        """The callback's: `login` replaces the session, in a new row under a new handle, so that the
        handle the pending login was reached by cannot reach it. The pending login's state is spent:
        of the requests replacing one row, only the first succeeds, and none once the row has ended."""
        self._payload.clear()
        self.replaced = NewLogin(login, absolute_expires_at)

    def end(self) -> None:
        self._payload.clear()
        self.replaced = Logout()


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
        row = await self._store.read(_row_id(handle), idle=self._idle, step=self._step) if handle is not None else None
        initial = copy.deepcopy(row.payload) if row is not None else {}
        request.scope["session"] = copy.deepcopy(initial)
        session = RequestSession(
            request.scope["session"],
            row.login if row is not None else None,
            SessionRow(self._store, row.id, idle=self._idle, step=self._step) if row is not None else None,
        )
        request.state.request_session = session
        response = await call_next(request)
        payload = request.session
        replaced = session.replaced
        if isinstance(replaced, Logout):
            if row is not None:
                await self._store.end(row.id)
            self._forget(response)
        elif isinstance(replaced, NewLogin):
            handle = await self._store.log_in(row.id if row is not None else None, payload, replaced, idle=self._idle)
            if handle is None:
                response = JSONResponse(
                    {"detail": "This login was completed or its session ended meanwhile; log in again."},
                    status_code=401,
                )
                self._forget(response)
            else:
                self._remember(response, handle, replaced.absolute_expires_at)
        elif payload != initial:
            if row is None:
                absolute = datetime.now(UTC) + min(_PENDING_LOGIN, timedelta(seconds=self._max_age))
                self._remember(response, await self._store.create(payload, absolute, idle=self._idle), absolute)
            elif not await self._store.save(row.id, initial, payload):
                self._forget(response)
        elif cookie is not None and row is None:
            self._forget(response)
        response.headers["Cache-Control"] = "no-store"
        return response

    def _remember(self, response: Response, handle: str, absolute_expires_at: datetime) -> None:
        response.set_cookie(
            self._cookie,
            self._signer.sign(handle).decode("ascii"),
            max_age=max(0, int((absolute_expires_at - datetime.now(UTC)).total_seconds())),
            httponly=True,
            secure=self._secure,
            samesite="lax",
            path="/",
        )

    def _forget(self, response: Response) -> None:
        response.delete_cookie(self._cookie, path="/", secure=self._secure, httponly=True, samesite="lax")
