"""Tests for HostexecClient — the in-process hostexec tool → hostexecd POST, over a respx mock.

respx patches httpx's transport, so the async POST is intercepted without a network hop. An
`AsyncMock` stands in for the per-host Authentik exchange.
"""

import json
from unittest.mock import AsyncMock, call, patch

import httpx
import pytest
import pytest_bazel
import respx
from fastmcp.exceptions import ToolError

from haku.console.tools.hostexec_client import _CONNECT_TIMEOUT_SECONDS, _DEFAULT_HTTP_TIMEOUT_SECONDS, HostexecClient
from mcp_infra.exec.models import BaseExecResult, Exited

EXEC_URLS = {"wyrm2": "http://wyrm2.mesh:8080", "rugged": "http://rugged.mesh:8080"}


def _exchange() -> AsyncMock:
    return AsyncMock(side_effect=lambda host, run_as: f"token-{host}-{run_as}")


def _client(exchange: AsyncMock | None = None) -> HostexecClient:
    return HostexecClient(exec_urls=EXEC_URLS, exchange=exchange or _exchange())


def _ok_result() -> BaseExecResult:
    return BaseExecResult(exit=Exited(exit_code=0), stdout="hello", stderr="", duration_ms=12)


async def test_run_posts_exchanged_token_and_returns_result() -> None:
    exchange = _exchange()
    with respx.mock:
        route = respx.post("http://wyrm2.mesh:8080/exec").mock(
            return_value=httpx.Response(200, json=_ok_result().model_dump())
        )
        result = await _client(exchange).run(
            host="wyrm2", run_as="root", argv=["echo", "hi"], cwd=None, max_bytes=1000, timeout_ms=5000
        )
    assert result == _ok_result()
    # The token was exchanged for exactly this (host, run_as) and posted in the body — never elsewhere.
    assert exchange.await_args_list == [call("wyrm2", "root")]
    body = json.loads(route.calls.last.request.content)
    assert body["token"] == "token-wyrm2-root"
    assert body["run_as"] == "root"
    assert body["argv"] == ["echo", "hi"]


async def test_run_rejects_host_out_of_scope() -> None:
    exchange = _exchange()
    with pytest.raises(ToolError, match="not in hostexec scope"):
        await _client(exchange).run(host="atlas", run_as="root", argv=["true"], cwd=None, max_bytes=0, timeout_ms=1000)
    # An out-of-scope host must be refused before any token is exchanged.
    exchange.assert_not_awaited()


async def test_run_surfaces_hostexecd_refusal() -> None:
    with respx.mock:
        respx.post("http://wyrm2.mesh:8080/exec").mock(return_value=httpx.Response(409, text="token already used"))
        with pytest.raises(ToolError, match=r"refused the call \(409\): token already used"):
            await _client().run(host="wyrm2", run_as="root", argv=["true"], cwd=None, max_bytes=0, timeout_ms=1000)


async def test_run_reports_unreachable_host() -> None:
    with respx.mock:
        respx.post("http://rugged.mesh:8080/exec").mock(side_effect=httpx.ConnectError("no route"))
        with pytest.raises(ToolError, match=r"'rugged' is unreachable"):
            await _client().run(
                host="rugged", run_as="agentydragon", argv=["true"], cwd=None, max_bytes=0, timeout_ms=1000
            )


async def test_run_splits_connect_and_read_timeouts() -> None:
    """An offline (roaming) host must fail fast on connect, so the connect timeout is short while the
    read timeout still outlasts a long-running command."""
    captured: list[httpx.Timeout] = []
    real_async_client = httpx.AsyncClient

    # run() constructs the client only as AsyncClient(timeout=...), so a timeout-only stand-in suffices.
    def capture(*, timeout: httpx.Timeout) -> httpx.AsyncClient:
        captured.append(timeout)
        return real_async_client(timeout=timeout)

    with respx.mock, patch.object(httpx, "AsyncClient", capture):
        respx.post("http://wyrm2.mesh:8080/exec").mock(return_value=httpx.Response(200, json=_ok_result().model_dump()))
        await _client().run(host="wyrm2", run_as="root", argv=["true"], cwd=None, max_bytes=0, timeout_ms=5000)

    [timeout] = captured
    assert timeout.connect == _CONNECT_TIMEOUT_SECONDS
    assert timeout.read == _DEFAULT_HTTP_TIMEOUT_SECONDS
    assert timeout.connect < timeout.read


if __name__ == "__main__":
    pytest_bazel.main()
