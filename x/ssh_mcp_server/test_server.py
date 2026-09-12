from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
import pytest_bazel
from fastmcp import Client
from fastmcp.exceptions import ToolError

from x.ssh_mcp_server import server as ssh_server
from x.ssh_mcp_server.server import SshSettings, TargetConfig, build_mcp, create_app, load_settings


def _settings(tmp_path: Path, *, identity: bool = True) -> SshSettings:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("host ssh-ed25519 AAAA\n")
    identity_file = tmp_path / "key"
    if identity:
        identity_file.write_text("not-a-real-key")
    return SshSettings(
        known_hosts_file=known_hosts,
        targets=[TargetConfig(host="host", user="coder", identity_file=identity_file)],
        command_timeout_seconds=3,
        output_limit_bytes=8,
    )


@pytest.mark.anyio
async def test_list_targets_keeps_missing_identity_unavailable(tmp_path: Path) -> None:
    settings = _settings(tmp_path, identity=False)
    async with Client(build_mcp(settings)) as client:
        result = await client.call_tool("list_targets", {})
    assert json.loads(result.content[0].text) == [
        {
            "host": "host",
            "user": "coder",
            "available": False,
            "error": {"kind": "identity_file_unavailable", "message": "configured SSH identity is unavailable"},
        }
    ]


@pytest.mark.anyio
async def test_exec_returns_bounded_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        ssh_server, "_run_ssh", lambda target, settings, command, timeout: (("01234567", True), ("err-outpu", True), 0)
    )
    async with Client(build_mcp(settings)) as client:
        result = await client.call_tool("exec", {"host": "host", "user": "coder", "command": "echo $HOME"})
    assert json.loads(result.content[0].text) == {
        "host": "host",
        "user": "coder",
        "exit_code": 0,
        "stdout": "01234567",
        "stderr": "err-outpu",
        "stdout_truncated": True,
        "stderr_truncated": True,
    }


@pytest.mark.anyio
async def test_exec_rejects_unknown_target_and_timeout_above_max(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    async with Client(build_mcp(settings)) as client:
        with pytest.raises(ToolError, match="target_not_configured"):
            await client.call_tool("exec", {"host": "other", "user": "coder", "command": "true"})
        with pytest.raises(ToolError, match="timeout_exceeds_maximum"):
            await client.call_tool("exec", {"host": "host", "user": "coder", "command": "true", "timeout_seconds": 4})


@pytest.mark.anyio
async def test_exec_timeout_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)

    def slow_run(*args: object) -> object:
        time.sleep(10)
        return (("", False), ("", False), 0)

    monkeypatch.setattr(ssh_server, "_run_ssh", slow_run)
    settings = settings.model_copy(update={"command_timeout_seconds": 1})
    async with Client(build_mcp(settings)) as client:
        with pytest.raises(ToolError, match="execution_unknown"):
            await client.call_tool("exec", {"host": "host", "user": "coder", "command": "sleep 10"})


def test_config_loads_and_rejects_duplicate_target(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(
        "known_hosts_file: /known_hosts\n"
        "targets:\n"
        "  - {host: host, user: coder, identity_file: /key}\n"
        "command_timeout_seconds: 10\n"
    )
    assert load_settings(path).command_timeout_seconds == 10
    with pytest.raises(ValueError, match="tuples must be unique"):
        SshSettings(
            known_hosts_file=Path("/known_hosts"),
            targets=[
                TargetConfig(host="host", user="coder", identity_file=Path("/one")),
                TargetConfig(host="host", user="coder", identity_file=Path("/two")),
            ],
        )


@pytest.mark.anyio
async def test_create_app_has_public_health_and_protected_mcp(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), "token")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.post("/mcp", json={"jsonrpc": "2.0"})).status_code == 401
        response = await client.post(
            "/mcp",
            headers={"Authorization": "Bearer token"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert response.status_code != 401


if __name__ == "__main__":
    pytest_bazel.main()
