"""Tool-surface tests for Console's in-process sandbox server."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest_bazel
from fastmcp import Client
from mcp.types import Tool

from haku.console.tools.sandbox import build_mcp
from haku.sandbox.config import SandboxEnvironmentConfig
from haku.sandbox.models import DisposeSandboxResult, SandboxExecResult, SandboxInfo, SandboxListPage, SandboxWarning
from mcp_infra.exec.models import Exited

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _environment(*, max_exec_timeout_seconds: int = 300, max_output_bytes: int = 100_000) -> SandboxEnvironmentConfig:
    return SandboxEnvironmentConfig.model_validate(
        {
            "sandbox": {
                "namespace": "agent-workspaces",
                "warm_pool": "haku",
                "container": "workspace",
                "default_cwd": "/workspace/haku-state",
                "initial_ttl_seconds": 28_800,
                "exec_ttl_extension_seconds": 7_200,
                "provisioning_timeout_seconds": 600,
                "max_exec_timeout_seconds": max_exec_timeout_seconds,
                "max_output_bytes": max_output_bytes,
            },
            "bootstrap": {"cwd": "/workspace", "timeout_seconds": 300, "script": "echo ready"},
        }
    )


def _info(name: str = "task-one", warnings: list[SandboxWarning] | None = None) -> SandboxInfo:
    return SandboxInfo(
        name=name,
        state="ready",
        healthy=True,
        expires_at=NOW,
        bootstrap_state="succeeded",
        sandbox_name="haku-abcde",
        pod_name="haku-abcde",
        warnings=warnings or [],
    )


def _client() -> Mock:
    client = Mock()
    client.provision = AsyncMock(return_value=_info())
    client.execute = AsyncMock(
        return_value=SandboxExecResult(
            exit=Exited(exit_code=0), stdout="ok\n", stderr="", duration_seconds=0.25, expires_at=NOW
        )
    )
    client.info = AsyncMock(return_value=_info())
    client.list = AsyncMock(return_value=SandboxListPage(sandboxes=[_info()]))
    client.dispose = AsyncMock(return_value=DisposeSandboxResult(name="task-one", deleted=True))
    return client


async def _tools(client: Mock, environment: SandboxEnvironmentConfig | None = None) -> dict[str, Tool]:
    async with Client(build_mcp(client, environment or _environment())) as mcp_client:
        return {tool.name: tool for tool in await mcp_client.list_tools()}


async def test_tool_surface_and_annotations() -> None:
    tools = await _tools(_client())

    assert set(tools) == {"provision_sandbox", "exec_sandbox", "get_sandbox_info", "list_sandboxes", "dispose_sandbox"}
    assert tools["get_sandbox_info"].annotations is not None
    assert tools["get_sandbox_info"].annotations.readOnlyHint
    assert tools["list_sandboxes"].annotations is not None
    assert tools["list_sandboxes"].annotations.readOnlyHint
    assert tools["provision_sandbox"].annotations is not None
    assert tools["provision_sandbox"].annotations.idempotentHint
    assert tools["dispose_sandbox"].annotations is not None
    assert tools["dispose_sandbox"].annotations.destructiveHint


async def test_exec_advertises_configured_maxes() -> None:
    tools = await _tools(_client(), _environment(max_exec_timeout_seconds=120, max_output_bytes=5_000))
    properties = tools["exec_sandbox"].inputSchema["properties"]

    assert properties["timeout_seconds"]["maximum"] == 120
    assert properties["max_output_bytes"]["maximum"] == 5_000


async def test_provision_has_no_profile_or_ttl_parameter() -> None:
    tools = await _tools(_client())
    properties = tools["provision_sandbox"].inputSchema["properties"]

    assert set(properties) == {"name"}


async def test_exec_dispatches_bash_inputs_in_seconds() -> None:
    client = _client()
    async with Client(build_mcp(client, _environment())) as mcp_client:
        result = await mcp_client.call_tool(
            "exec_sandbox",
            {
                "name": "task-one",
                "script": "git status --short",
                "cwd": "/workspace/haku-state",
                "timeout_seconds": 30,
                "max_output_bytes": 4096,
            },
        )

    assert not result.is_error
    assert result.data.duration_seconds == 0.25
    client.execute.assert_awaited_once_with(
        name="task-one",
        script="git status --short",
        cwd="/workspace/haku-state",
        timeout_seconds=30,
        max_output_bytes=4096,
    )


async def test_divergence_reaches_the_agent_as_a_non_fatal_warning() -> None:
    warning = SandboxWarning(kind="bootstrap_script_changed", detail="claim records bootstrap script digest 'abc'")
    client = _client()
    client.info = AsyncMock(return_value=_info(warnings=[warning]))

    async with Client(build_mcp(client, _environment())) as mcp_client:
        result = await mcp_client.call_tool("get_sandbox_info", {"name": "task-one"})

    assert not result.is_error
    assert result.data.state == "ready"
    assert [(entry.kind, entry.detail) for entry in result.data.warnings] == [(warning.kind, warning.detail)]


if __name__ == "__main__":
    pytest_bazel.main()
