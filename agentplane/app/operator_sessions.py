"""PostgreSQL browser sessions; only a random, signed handle crosses into the browser.

Authlib's state/nonce/PKCE data and the operator access token stay in the same server-side row.
A row lock serializes concurrent callbacks/logout across replicas. Commit before sending headers,
not after streaming the response, so logout cannot be undone by an older in-flight save.
"""

from __future__ import annotations

import copy
import hashlib
import secrets
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from itsdangerous import BadSignature, TimestampSigner
from sqlalchemy import DateTime, Text, delete, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp


class Base(DeclarativeBase):
    pass


class BrowserSession(Base):
    __tablename__ = "operator_browser_session"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class OperatorSessionStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)


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
    ) -> None:
        super().__init__(app)
        self._store = store
        self._signer = TimestampSigner(secret_key, salt="agentplane-operator-session-v1")
        self._cookie = session_cookie
        self._secure = https_only
        self._max_age = max_age

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        handle: str | None = None
        cookie = request.cookies.get(self._cookie)
        if cookie is not None:
            with suppress(BadSignature, UnicodeError):
                handle = self._signer.unsign(cookie, max_age=self._max_age).decode("ascii")
        async with self._store.sessions.begin() as db:
            row = None
            if handle is not None:
                row = await db.scalar(
                    select(BrowserSession)
                    .where(BrowserSession.id == hashlib.sha256(handle.encode()).hexdigest())
                    .with_for_update()
                )
                if row is not None and row.expires_at <= datetime.now(UTC):
                    await db.delete(row)
                    row = None
            initial = copy.deepcopy(row.payload) if row is not None else {}
            request.scope["session"] = copy.deepcopy(initial)
            request.state.rotate_operator_session = False
            request.state.operator_session_expires_at = None
            response = await call_next(request)
            payload = request.session
            rotate = request.state.rotate_operator_session
            changed = payload != initial or rotate
            if changed:
                if row is not None and (not payload or rotate):
                    await db.delete(row)
                    row = None
                if payload:
                    expiry = request.state.operator_session_expires_at
                    if row is None:
                        handle = secrets.token_urlsafe(32)
                        row = BrowserSession(
                            id=hashlib.sha256(handle.encode()).hexdigest(),
                            payload=payload,
                            expires_at=expiry or datetime.now(UTC) + timedelta(seconds=min(600, self._max_age)),
                        )
                        db.add(row)
                    else:
                        row.payload = payload
                        if expiry is not None:
                            row.expires_at = expiry
                    assert handle is not None
                    response.set_cookie(
                        self._cookie,
                        self._signer.sign(handle).decode("ascii"),
                        max_age=max(0, int((row.expires_at - datetime.now(UTC)).total_seconds())),
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
        response.headers["Cache-Control"] = "no-store"
        return response
