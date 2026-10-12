"""Channels and authenticated unary calls for the Sandbox Service and Runner Gateway clients, with
status codes translated in one place."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Never

import grpc
from google.protobuf.message import Message

from agentplane.grpc_options import LARGE_EVENT_RECEIVE_OPTIONS
from agentplane.runner.errors import RunnerError

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


class ServiceError(ConnectionError):
    def __init__(self, code: grpc.StatusCode, details: str | None) -> None:
        super().__init__(f"{code.name}: {details}")
        self.code = code


def dial(target: str) -> grpc.aio.Channel:
    # No retry service config: uncertain mutations need domain-specific reconciliation.
    # A local subchannel pool makes each per-call control channel dial anew; from the process-wide
    # pool it would inherit a closed channel's connection to a replica that no longer holds the runner.
    return grpc.aio.insecure_channel(
        target,
        options=(*LARGE_EVENT_RECEIVE_OPTIONS, ("grpc.enable_retries", 0), ("grpc.use_local_subchannel_pool", 1)),
    )


def _raise(error: grpc.aio.AioRpcError) -> Never:
    if error.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
        raise TimeoutError(f"{error.code().name}: {error.details()}") from error
    if error.code() == grpc.StatusCode.FAILED_PRECONDITION:
        raise RunnerError(f"{error.code().name}: {error.details()}") from error
    raise ServiceError(error.code(), error.details()) from error


async def bearer(token_file: Path) -> tuple[tuple[str, str], ...]:
    # Projected tokens rotate. Read for every RPC.
    token = (await asyncio.to_thread(token_file.read_text)).strip()
    return (("authorization", f"Bearer {token}"),)


async def call_unary[T](
    call: Callable[..., Awaitable[T]], request: Message, *, token_file: Path, timeout_s: float
) -> T:
    try:
        return await call(request, metadata=await bearer(token_file), timeout=timeout_s)
    except grpc.aio.AioRpcError as error:
        _raise(error)
