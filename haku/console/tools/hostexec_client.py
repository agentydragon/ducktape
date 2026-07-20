"""Dispatcher from the in-process `hostexec` tool to an outbound node daemon.

On each approved call it exchanges the operator's identity for a short-lived, single-use per-host
Authentik token (via the injected `exchange` callable), durably queues a `HostexecRequest`, and
waits for the configured daemon to claim it over its outbound console session. Host scope is the
configured host-to-daemon map — a host not in it is refused. An offline roaming host is a normal,
fast `ToolError`, not a crash. The operator token is never logged and is delivered only in the
claimed execution body to the host that verifies it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol
from uuid import UUID

from fastmcp.exceptions import ToolError

from haku.hostexec.wire import HostexecRequest
from mcp_infra.exec.models import BaseExecResult

logger = logging.getLogger(__name__)

# Exchanges the operator's identity for a short-lived, single-use per-host token for one approved
# call: (host, run_as) -> token (`aud=hostexec-<host>`, carrying the operator's `hostexec-*` group
# claims). Raises on failure — never returns an empty token. `HostexecJwtBearerExchanger.exchange`
# is the production impl.
ExchangeToken = Callable[[str, str], Awaitable[str]]


class NodeDaemonBroker(Protocol):
    def enqueue(self, *, daemon_id: str, backend: str, payload: dict[str, object]) -> UUID: ...

    async def wait(self, execution_id: UUID) -> dict[str, object]: ...


class HostexecClient:
    """Queues an approved command for a connected hostexecd and waits for its durable result."""

    def __init__(self, *, daemon_ids: Mapping[str, str], exchange: ExchangeToken, broker: NodeDaemonBroker) -> None:
        self._daemon_ids = dict(daemon_ids)
        self._exchange = exchange
        self._broker = broker

    async def run(
        self, *, host: str, run_as: str, cmd: str, cwd: str | None, max_bytes: int, timeout_ms: int
    ) -> BaseExecResult:
        daemon_id = self._daemon_ids.get(host)
        if daemon_id is None:
            raise ToolError(f"host {host!r} is not in hostexec scope (configured: {sorted(self._daemon_ids)})")
        token = await self._exchange(host, run_as)
        request = HostexecRequest(
            token=token, run_as=run_as, cmd=cmd, cwd=cwd, max_bytes=max_bytes, timeout_ms=timeout_ms
        )
        # Audit to stdout in the haku-console namespace (Haku can't read these logs); never the token.
        logger.info("hostexec run host=%s run_as=%s cmd=%r", host, run_as, cmd[:200])
        try:
            execution_id = self._broker.enqueue(daemon_id=daemon_id, backend="hostexec", payload=request.model_dump())
            result = await self._broker.wait(execution_id)
        except RuntimeError as error:
            raise ToolError(str(error)) from error
        return BaseExecResult.model_validate(result)
