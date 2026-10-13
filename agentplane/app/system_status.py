"""Whether each service the app depends on answers, for the System page."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import grpc
import httpx
from grpc_health.v1 import health_pb2, health_pb2_grpc
from pydantic import AwareDatetime, BaseModel, Field

# The generated health stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)


class ComponentState(StrEnum):
    OK = "ok"
    DOWN = "down"


class ComponentStatus(BaseModel):
    name: str = Field(description="The component, as the System page labels it.")
    state: ComponentState
    detail: str | None = Field(description="Why the component is down; absent when it is ok.")
    checked_at: AwareDatetime


@dataclass(frozen=True)
class Probe:
    """One component's check: it returns when the component is healthy and raises when it is not."""

    name: str
    check: Callable[[], Awaitable[None]]


def grpc_health_probe(name: str, channel: grpc.aio.Channel) -> Probe:
    async def check() -> None:
        response = await health_pb2_grpc.HealthStub(channel).Check(health_pb2.HealthCheckRequest())
        if response.status != health_pb2.HealthCheckResponse.SERVING:
            raise ConnectionError(health_pb2.HealthCheckResponse.ServingStatus.Name(response.status))

    return Probe(name, check)


def http_health_probe(name: str, client: httpx.AsyncClient, path: str) -> Probe:
    async def check() -> None:
        (await client.get(path)).raise_for_status()

    return Probe(name, check)


def _detail(error: Exception, timeout_s: float) -> str:
    if isinstance(error, grpc.aio.AioRpcError):
        return f"{error.code().name}: {error.details()}"
    if isinstance(error, TimeoutError):
        return f"no answer within {timeout_s:g}s"
    return f"{type(error).__name__}: {error}"


class SystemStatusChecker:
    def __init__(self, probes: Sequence[Probe], *, timeout_s: float) -> None:
        self._probes = probes
        self._timeout_s = timeout_s

    async def _check(self, probe: Probe) -> ComponentStatus:
        detail: str | None = None
        try:
            async with asyncio.timeout(self._timeout_s):
                await probe.check()
        except Exception as error:
            logger.warning("system status: %s is down", probe.name, exc_info=True)
            detail = _detail(error, self._timeout_s)
        return ComponentStatus(
            name=probe.name,
            state=ComponentState.OK if detail is None else ComponentState.DOWN,
            detail=detail,
            checked_at=datetime.now(UTC),
        )

    async def check_all(self) -> list[ComponentStatus]:
        return list(await asyncio.gather(*(self._check(probe) for probe in self._probes)))
