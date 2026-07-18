"""HTTP client from the in-process `hostexec` console tool to a host's `hostexecd`.

On each approved call it exchanges the operator's identity for a short-lived, single-use per-host
Authentik token (via the injected `exchange` callable), POSTs a `HostexecRequest` to that host's
`hostexecd` over the pod network, and returns the `BaseExecResult`. Host scope is the configured
exec-URL map — a host not in it is refused. A roaming host (e.g. `rugged`) being offline is a normal,
fast `ToolError`, not a crash. The token never leaves this process except in the request body to the
host that will verify it; it is never logged.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping

import httpx
from fastmcp.exceptions import ToolError

from haku.hostexec.wire import HostexecRequest
from mcp_infra.exec.models import MAX_EXEC_TIMEOUT_MS, BaseExecResult

logger = logging.getLogger(__name__)

# Exchanges the operator's identity for a short-lived, single-use per-host token for one approved
# call: (host, run_as) -> token (`aud=hostexec-<host>`, carrying the operator's `hostexec-*` group
# claims). Raises on failure — never returns an empty token. `HostexecJwtBearerExchanger.exchange`
# is the production impl.
ExchangeToken = Callable[[str, str], Awaitable[str]]

# hostexecd responds only once the command finishes or its own timeout_ms fires (capped at
# MAX_EXEC_TIMEOUT_MS), so the default HTTP wait must outlast the longest command plus margin.
_DEFAULT_HTTP_TIMEOUT_SECONDS = MAX_EXEC_TIMEOUT_MS / 1000 + 30


class HostexecClient:
    """POSTs an approved command to a host's `hostexecd`, exchanging for the per-host token per call."""

    def __init__(
        self, *, exec_urls: Mapping[str, str], exchange: ExchangeToken, timeout: float = _DEFAULT_HTTP_TIMEOUT_SECONDS
    ) -> None:
        self._exec_urls = dict(exec_urls)
        self._exchange = exchange
        self._timeout = timeout

    async def run(
        self, *, host: str, run_as: str, argv: list[str], cwd: str | None, max_bytes: int, timeout_ms: int
    ) -> BaseExecResult:
        exec_url = self._exec_urls.get(host)
        if exec_url is None:
            raise ToolError(f"host {host!r} is not in hostexec scope (configured: {sorted(self._exec_urls)})")
        token = await self._exchange(host, run_as)
        request = HostexecRequest(
            token=token, run_as=run_as, argv=argv, cwd=cwd, max_bytes=max_bytes, timeout_ms=timeout_ms
        )
        # Audit to stdout in the haku-console namespace (Haku can't read these logs); never the token.
        logger.info("hostexec run host=%s run_as=%s argv0=%s", host, run_as, argv[0])
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                response = await http.post(f"{exec_url.rstrip('/')}/exec", json=request.model_dump())
        except httpx.TransportError as error:
            # Roaming hosts are frequently offline; a fast, clear failure, not a crash.
            raise ToolError(f"host {host!r} is unreachable: {error}") from error
        if response.status_code != 200:
            # hostexecd refuses with a reason: 401 token / 403 group / 409 replay / 422 no such user.
            raise ToolError(f"hostexecd on {host!r} refused the call ({response.status_code}): {response.text[:300]}")
        return BaseExecResult.model_validate(response.json())
