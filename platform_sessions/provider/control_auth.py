"""Gateway authentication for the provider's private unary gRPC control API."""

from __future__ import annotations

import asyncio
import hmac
from pathlib import Path

import grpc

_CONTROL_PREFIX = "/platform_sessions.provider.v1.SessionProviderControl/"


class ControlBearerInterceptor(grpc.aio.ServerInterceptor):
    """Require the gateway's file-backed bearer on every provider control RPC.

    The file is read for each call so a projected Kubernetes secret can rotate
    without restarting the provider. Missing, unreadable, or malformed secrets
    fail closed. This authenticates the caller but does not encrypt traffic or
    authenticate the server; the server and channel must also use TLS.

    The interceptor protects the control service only. Other services, including
    health, need their own access policy. All control methods are unary.
    """

    def __init__(self, token_file: Path, *, service_prefix: str = _CONTROL_PREFIX):
        self._token_file = token_file
        self._service_prefix = service_prefix

    async def intercept_service(self, continuation, handler_call_details):
        handler = await continuation(handler_call_details)
        if handler is None or not handler_call_details.method.startswith(self._service_prefix):
            return handler
        # Fail closed if a future control service adds streaming without updating
        # this interceptor's denial handler.
        if handler.request_streaming or handler.response_streaming:
            raise RuntimeError("Provider control service must use unary RPCs")
        try:
            configured = await asyncio.to_thread(self._token_file.read_text, encoding="ascii")
        except OSError, UnicodeError:
            configured = ""
        configured = configured.strip()
        metadata = [value for key, value in handler_call_details.invocation_metadata if key == "authorization"]
        valid = (
            len(configured) >= 32
            and configured.isascii()
            and all(32 < ord(char) < 127 for char in configured)
            and len(metadata) == 1
            and isinstance(metadata[0], str)
            and metadata[0].isascii()
            and hmac.compare_digest(metadata[0], "Bearer " + configured)
        )
        if valid:
            return handler

        async def deny(request, context):
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Gateway authentication required")

        return grpc.unary_unary_rpc_method_handler(
            deny, request_deserializer=handler.request_deserializer, response_serializer=handler.response_serializer
        )
