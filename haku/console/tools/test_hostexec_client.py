"""Tests for HostexecClient's durable node-daemon dispatch boundary."""

from typing import Any
from unittest.mock import AsyncMock, call
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from fastmcp.exceptions import ToolError

from haku.console.tools.hostexec_client import HostexecClient
from mcp_infra.exec.models import BaseExecResult, Exited

DAEMON_IDS = {"wyrm2": "wyrm2", "rugged": "rugged"}


def _exchange() -> AsyncMock:
    return AsyncMock(side_effect=lambda host, run_as: f"token-{host}-{run_as}")


class Broker:
    def __init__(self, result: dict[str, Any] | None = None, error: str | None = None) -> None:
        self.execution_id = uuid4()
        self.enqueued: list[tuple[str, str, dict[str, Any]]] = []
        self.result = result or _ok_result().model_dump()
        self.error = error

    def enqueue(self, *, daemon_id: str, backend: str, payload: dict[str, Any]) -> UUID:
        if self.error:
            raise RuntimeError(self.error)
        self.enqueued.append((daemon_id, backend, payload))
        return self.execution_id

    async def wait(self, execution_id: UUID) -> dict[str, Any]:
        assert execution_id == self.execution_id
        return self.result


def _client(exchange: AsyncMock | None = None, broker: Broker | None = None) -> HostexecClient:
    return HostexecClient(daemon_ids=DAEMON_IDS, exchange=exchange or _exchange(), broker=broker or Broker())


def _ok_result() -> BaseExecResult:
    return BaseExecResult(exit=Exited(exit_code=0), stdout="hello", stderr="", duration_ms=12)


async def test_run_queues_exchanged_token_and_returns_result() -> None:
    exchange = _exchange()
    broker = Broker()
    result = await _client(exchange, broker).run(
        host="wyrm2", run_as="root", argv=["echo", "hi"], cwd=None, max_bytes=1000, timeout_ms=5000
    )
    assert result == _ok_result()
    assert exchange.await_args_list == [call("wyrm2", "root")]
    [(daemon_id, backend, payload)] = broker.enqueued
    assert (daemon_id, backend) == ("wyrm2", "hostexec")
    assert payload["token"] == "token-wyrm2-root"
    assert payload["run_as"] == "root"
    assert payload["argv"] == ["echo", "hi"]


async def test_run_rejects_host_out_of_scope_before_exchange() -> None:
    exchange = _exchange()
    with pytest.raises(ToolError, match="not in hostexec scope"):
        await _client(exchange).run(host="atlas", run_as="root", argv=["true"], cwd=None, max_bytes=0, timeout_ms=1000)
    exchange.assert_not_awaited()


async def test_run_surfaces_disconnected_daemon() -> None:
    with pytest.raises(ToolError, match="not connected"):
        await _client(broker=Broker(error="node daemon 'rugged' is not connected")).run(
            host="rugged", run_as="agentydragon", argv=["true"], cwd=None, max_bytes=0, timeout_ms=1000
        )


if __name__ == "__main__":
    pytest_bazel.main()
