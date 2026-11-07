"""Bounded lifecycle for the TLS-protected provider control gRPC server."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

import grpc

from platform_sessions.provider.control_auth import ControlBearerInterceptor

_RegisterServices = Callable[[grpc.aio.Server], None]


class SecureControlServer:
    """A provider control server that only binds with TLS and bearer authentication.

    The caller supplies PEM certificate and private key bytes, a mounted bearer token file, and a
    callback that registers service handlers. The helper owns the listener and never creates an
    insecure port. The token interceptor reads the file on every RPC, so projected-token
    replacement takes effect without restarting the process.

    Bounded gRPC shutdown only drains RPC transport work. It does not establish that dispatched
    provider operations completed safely or provide fenced ownership. Callers must drain or
    serialize provider work themselves, or persist durable uncertainty markers before process exit.
    """

    def __init__(
        self,
        *,
        bind_address: str,
        token_file: Path,
        certificate_chain_pem: bytes,
        private_key_pem: bytes,
        register_services: _RegisterServices,
        startup_timeout_seconds: float = 5.0,
        shutdown_grace_seconds: float = 5.0,
        shutdown_timeout_seconds: float = 6.0,
    ) -> None:
        host, separator, port = bind_address.rpartition(":")
        if not separator or not host or not port.isdecimal() or not 0 <= int(port) <= 65535:
            raise ValueError("bind_address must be a host:port TCP address")
        if not certificate_chain_pem or not private_key_pem:
            raise ValueError("TLS certificate and private key are required")
        if not math.isfinite(startup_timeout_seconds) or startup_timeout_seconds <= 0:
            raise ValueError("startup timeout must be finite and positive")
        if not math.isfinite(shutdown_grace_seconds) or shutdown_grace_seconds < 0:
            raise ValueError("shutdown grace must be finite and nonnegative")
        if not math.isfinite(shutdown_timeout_seconds) or shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown timeout must be finite and positive")
        if shutdown_grace_seconds >= shutdown_timeout_seconds:
            raise ValueError("shutdown timeout must exceed shutdown grace")

        self._bind_address = bind_address
        self._host = host
        self._token_file = token_file
        self._server_credentials = grpc.ssl_server_credentials(((private_key_pem, certificate_chain_pem),))
        self._register_services = register_services
        self._startup_timeout_seconds = startup_timeout_seconds
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._server: grpc.aio.Server | None = None
        self._bound_port: int | None = None
        self._state = "created"
        self._lifecycle_lock = asyncio.Lock()

    @property
    def target(self) -> str:
        """Dialable host and assigned port after the server has started."""
        if self._bound_port is None:
            raise RuntimeError("control server has not started")
        return f"{self._host}:{self._bound_port}"

    async def start(self) -> SecureControlServer:
        """Register services, bind a TLS-only listener, and start within the configured bound."""
        async with self._lifecycle_lock:
            if self._state == "started":
                return self
            if self._state != "created":
                raise RuntimeError("control server cannot be restarted")

            self._state = "starting"
            server = grpc.aio.server(interceptors=[ControlBearerInterceptor(self._token_file)])
            try:
                self._register_services(server)
                try:
                    bound_port = server.add_secure_port(self._bind_address, self._server_credentials)
                except RuntimeError:
                    # grpcio currently raises here for bind failures, despite the documented 0
                    # return value. Keep the error stable and avoid forwarding transport details.
                    raise OSError("could not bind TLS provider control listener") from None
                if bound_port == 0:
                    raise OSError("could not bind TLS provider control listener")
                await asyncio.wait_for(server.start(), timeout=self._startup_timeout_seconds)
            except BaseException:
                # The original bind or startup error is the useful and sanitized failure.
                with suppress(Exception):
                    await asyncio.wait_for(server.stop(grace=0), timeout=self._shutdown_timeout_seconds)
                self._state = "stopped"
                raise

            self._server = server
            self._bound_port = bound_port
            self._state = "started"
            return self

    async def stop(self) -> None:
        """Drain active calls for a bounded grace period, then close the listener."""
        async with self._lifecycle_lock:
            if self._state == "stopped":
                return
            if self._state == "created":
                self._state = "stopped"
                return
            if self._state != "started" or self._server is None:
                raise RuntimeError("control server is already stopping")

            server = self._server
            self._state = "stopping"
            try:
                await asyncio.wait_for(
                    server.stop(grace=self._shutdown_grace_seconds), timeout=self._shutdown_timeout_seconds
                )
                await asyncio.wait_for(server.wait_for_termination(), timeout=self._shutdown_timeout_seconds)
            except BaseException:
                with suppress(Exception):
                    await asyncio.wait_for(server.stop(grace=0), timeout=self._shutdown_timeout_seconds)
                    await asyncio.wait_for(server.wait_for_termination(), timeout=self._shutdown_timeout_seconds)
                self._state = "stopped"
                raise
            self._state = "stopped"

    async def __aenter__(self) -> SecureControlServer:
        return await self.start()

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        del exc_type, exc_value, traceback
        await self.stop()
