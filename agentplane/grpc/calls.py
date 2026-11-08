"""Bearer metadata and status translation for Agentplane gRPC clients, including Runner Gateway
calls that read FAILED_PRECONDITION as a `RefusedError`."""

import asyncio
import ipaddress
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Never

import grpc
from google.protobuf.message import Message

from agentplane.grpc.options import LARGE_EVENT_RECEIVE_OPTIONS

logger = logging.getLogger(__name__)

_DNS_NAME = re.compile(
    r"^(?=.{1,253}\.?$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$"
)


class RefusedError(Exception):
    """The runner or the Sandbox's state refused the operation (FAILED_PRECONDITION)."""


class ServiceError(ConnectionError):
    def __init__(
        self, code: grpc.StatusCode, details: str | None, trailing_metadata: grpc.aio.Metadata | None = None
    ) -> None:
        super().__init__(f"{code.name}: {details}")
        self.code = code
        self.details = details
        self.trailing_metadata = trailing_metadata


def dial(
    target: str, *, channel_credentials: grpc.ChannelCredentials | None = None, server_name: str | None = None
) -> grpc.aio.Channel:
    # No retry service config: uncertain mutations need domain-specific reconciliation.
    # A local subchannel pool makes each per-call control channel dial anew; from the process-wide
    # pool it would inherit a closed channel's connection to a replica that no longer holds the runner.
    if server_name is not None:
        if not _DNS_NAME.fullmatch(server_name):
            raise ValueError("server_name must be a DNS name, never a URI or address")
        try:
            ipaddress.ip_address(server_name)
        except ValueError:
            pass
        else:
            raise ValueError("server_name must be a DNS name, never a URI or address")

    options: list[tuple[str, int | str]] = [
        *LARGE_EVENT_RECEIVE_OPTIONS,
        ("grpc.enable_retries", 0),
        ("grpc.use_local_subchannel_pool", 1),
    ]
    if server_name is not None:
        # gRPC still validates the certificate chain and matches this DNS name against its SANs.
        options.extend([("grpc.ssl_target_name_override", server_name), ("grpc.default_authority", server_name)])
    if channel_credentials is None:
        if server_name is not None:
            raise ValueError("server_name requires TLS channel credentials")
        return grpc.aio.insecure_channel(target, options=options)
    return grpc.aio.secure_channel(target, channel_credentials, options=options)


def _log_failure(error: grpc.aio.AioRpcError) -> None:
    logger.warning("call failed with %s (%s)", error.code().name, error.details(), exc_info=error)


def raise_status(error: grpc.aio.AioRpcError) -> Never:
    _log_failure(error)
    if error.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
        raise TimeoutError(f"{error.code().name}: {error.details()}") from error
    raise ServiceError(error.code(), error.details(), error.trailing_metadata()) from error


def _raise(error: grpc.aio.AioRpcError) -> Never:
    if error.code() == grpc.StatusCode.FAILED_PRECONDITION:
        _log_failure(error)
        raise RefusedError(f"{error.code().name}: {error.details()}") from error
    raise_status(error)


async def bearer(token_file: Path) -> tuple[tuple[str, str], ...]:
    # Projected tokens rotate. Read for every RPC.
    token = (await asyncio.to_thread(token_file.read_text)).strip()
    return (("authorization", f"Bearer {token}"),)


async def call_unary[T](
    call: Callable[..., Awaitable[T]],
    request: Message,
    *,
    token_file: Path,
    timeout_s: float,
    extra_metadata: tuple[tuple[str, str], ...] = (),
) -> T:
    try:
        return await call(request, metadata=(*await bearer(token_file), *extra_metadata), timeout=timeout_s)
    except grpc.aio.AioRpcError as error:
        _raise(error)
