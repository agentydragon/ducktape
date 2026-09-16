"""The caller check a gRPC server runs for itself -- written the wrong way on purpose to show it.

This reimplements what `require_caller` already decides, because `require_caller` takes a Starlette
`Request` and reads `request.scope["session"]`, which `OperatorSessionMiddleware` populates: the
cookie work lives inside a middleware rather than in a function an interceptor could call. Taking
the shortcut was a mistake, and README.md finding 3 says what should happen instead -- lift that
read into a shared function, or let Envoy `ext_authz` ask the app itself. Do not take the line
count here as a cost of gRPC-Web.

What no refactor fixes: `OperatorSessionMiddleware` also *writes*, rotating handles and refreshing
expiry on the response, and an interceptor has no response to do that on. So a session the
browser's REST calls refresh is not refreshed by its RPC calls. `ext_authz` avoids that too.

Spike. Findings are in README.md.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from typing import Any

import grpc
from itsdangerous import BadSignature, TimestampSigner
from sqlalchemy import select

from x.agentplane.app.identity import CallerIdentity, CallerKind
from x.agentplane.app.oidc import OperatorSession
from x.agentplane.app.operator_sessions import BrowserSession, OperatorSessionStore

logger = logging.getLogger(__name__)

# Every gRPC call is a POST, so the Origin check `require_caller` applies to unsafe methods applies
# to all of them. Envoy forwards the browser's Origin header as call metadata.


class OperatorInterceptor(grpc.aio.ServerInterceptor):
    def __init__(
        self, store: OperatorSessionStore, *, secret_key: str, session_cookie: str, max_age: int, public_base_url: str
    ) -> None:
        self._store = store
        # The same salt as OperatorSessionMiddleware. A drift here silently refuses every browser.
        self._signer = TimestampSigner(secret_key, salt="agentplane-operator-session-v1")
        self._cookie = session_cookie
        self._max_age = max_age
        self._origin = public_base_url.rstrip("/")

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler | None]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler | None:
        metadata = dict(handler_call_details.invocation_metadata or ())
        caller = await self._caller(metadata)
        if caller is None:
            return _refuse(grpc.StatusCode.UNAUTHENTICATED, "no session and no accepted token")
        if caller.kind is CallerKind.OPERATOR and metadata.get("origin") != self._origin:
            return _refuse(grpc.StatusCode.PERMISSION_DENIED, f"cross-origin call from {metadata.get('origin')!r}")
        return await continuation(handler_call_details)

    async def _caller(self, metadata: dict[str, Any]) -> CallerIdentity | None:
        """`session_operator` -> `operator_session`, minus the Request it reads them off."""
        jar = SimpleCookie()
        jar.load(str(metadata.get("cookie", "")))
        signed = jar[self._cookie].value if self._cookie in jar else None
        if signed is None:
            return None
        try:
            handle = self._signer.unsign(signed, max_age=self._max_age).decode("ascii")
        except BadSignature, UnicodeError:
            return None
        async with self._store.sessions.begin() as db:
            row = await db.scalar(
                select(BrowserSession).where(BrowserSession.id == hashlib.sha256(handle.encode()).hexdigest())
            )
            if row is None or row.expires_at <= datetime.now(UTC):
                return None
            session = OperatorSession.model_validate(row.payload["user"])
        if session.expires_at <= datetime.now(UTC).timestamp():
            return None
        return CallerIdentity(CallerKind.OPERATOR, session.username)


def _refuse(code: grpc.StatusCode, message: str) -> grpc.RpcMethodHandler:
    async def deny(_request: object, context: grpc.aio.ServicerContext) -> None:
        await context.abort(code, message)

    return grpc.unary_unary_rpc_method_handler(deny)
