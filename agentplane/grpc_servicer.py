"""What every agentplane servicer shares: admitting its callers and mapping failures to status codes."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import grpc
from kubernetes_asyncio import client as k8s_client

from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.grpc_caller import authenticate
from agentplane.workload_auth.principal import WorkloadPrincipalRejectedError, WorkloadPrincipalResolver

logger = logging.getLogger(__name__)

type Status = tuple[grpc.StatusCode, str]

# Failures on the server's side of the call, whose cause the fixed client message does not carry.
_LOGGED = frozenset({grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED})


@dataclass(frozen=True)
class ServicerResources:
    """The fields every servicer's `Resources` starts with."""

    principals: WorkloadPrincipalResolver
    caller_accounts: frozenset[ServiceAccountRef]

    def __post_init__(self) -> None:
        if not self.caller_accounts:
            raise ValueError("at least one service caller is required")

    async def authenticate(self, context: grpc.aio.ServicerContext) -> ServiceAccountRef:
        return await authenticate(self.principals, self.caller_accounts, context)


@dataclass(frozen=True)
class Abort[E: Exception]:
    """Aborts an RPC that raised one of `errors` with `status`, or with the status derived from the error."""

    errors: type[E] | tuple[type[E], ...]
    status: Status | Callable[[E], Status]


def status_mapping(
    *rows: Abort[Any], deadline: str, unavailable: str, also_unavailable: tuple[type[Exception], ...] = ()
) -> Callable[[grpc.aio.ServicerContext], AbstractAsyncContextManager[None]]:
    """A servicer's `errors(context)`: the first matching row aborts. A rejected bearer, a deadline and
    an unreachable backend map the same way everywhere; `rows` go between them. An abort with
    `UNAVAILABLE` or `DEADLINE_EXCEEDED` logs the exception behind it."""
    table = (
        Abort(WorkloadPrincipalRejectedError, (grpc.StatusCode.UNAUTHENTICATED, "invalid workload bearer")),
        *rows,
        Abort(TimeoutError, (grpc.StatusCode.DEADLINE_EXCEEDED, deadline)),
        Abort(
            (ConnectionError, k8s_client.ApiException, *also_unavailable), (grpc.StatusCode.UNAVAILABLE, unavailable)
        ),
    )

    @asynccontextmanager
    async def errors(context: grpc.aio.ServicerContext) -> AsyncIterator[None]:
        try:
            yield
        except Exception as error:
            for row in table:
                if isinstance(error, row.errors):
                    code, details = row.status(error) if callable(row.status) else row.status
                    if code in _LOGGED:
                        logger.warning("answered %s (%s)", code.name, details, exc_info=error)
                    await context.abort(code, details)
            raise

    return errors
