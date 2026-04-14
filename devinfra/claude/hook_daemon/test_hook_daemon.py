"""Unit tests for hook daemon server — exercises the FastAPI app in-process."""

import json
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_bazel
from httpx import ASGITransport, AsyncClient
from pydantic import TypeAdapter

from devinfra.claude.claude_api.hooks.post_tool_use import PostToolUseInput
from devinfra.claude.claude_api.hooks.pre_tool_use import PreToolUseInput
from devinfra.claude.claude_api.hooks.stop import StopInput
from devinfra.claude.hook_daemon.config import ProfileConfig
from devinfra.claude.hook_daemon.models import (
    HookRequest,
    HookResponse,
    ShimBlocked,
    ShimExecRequest,
    ShimExecve,
    StartupResult,
)
from devinfra.claude.hook_daemon.server import create_app
from devinfra.claude.hook_daemon.testing.testing_helpers import TEST_PROFILE

_PERMISSIVE_PROFILE = ProfileConfig(idle_watchdog=True)

_COMMON = {
    "session_id": "test-session",
    "transcript_path": "/tmp/transcript.jsonl",
    "cwd": "/tmp",
    "permission_mode": "default",
}

_JSON_HEADERS = {"Content-Type": "application/json"}


@pytest.fixture
async def client(tmp_path: Path) -> AsyncGenerator[AsyncClient]:
    """Create an async test client for the daemon app."""
    daemon_dir = tmp_path / "hook-daemon"
    daemon_dir.mkdir()
    app = create_app(daemon_dir, profile=TEST_PROFILE, startup=StartupResult())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    env_file = tmp_path / "session-env" / "test" / "sessionstart-hook-0.sh"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    return {"HOME": "/tmp", "PATH": "/usr/bin", "CLAUDE_ENV_FILE": str(env_file), "CLAUDE_PROJECT_DIR": str(tmp_path)}


async def _post_hook(client: AsyncClient, req: HookRequest) -> HookResponse:
    """POST a hook request and return parsed response, asserting 200."""
    resp = await client.post("/hook", content=req.model_dump_json(), headers=_JSON_HEADERS)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    return HookResponse.model_validate_json(resp.content)


class TestHandleHook:
    async def test_pre_tool_use_allowed_command(self, client: AsyncClient, env: dict[str, str]) -> None:
        """PreToolUse for an allowed bash command returns approve decision."""
        hook_input = PreToolUseInput(
            **_COMMON,
            hook_event_name="PreToolUse",
            tool_use_id="toolu_test",
            tool_name="Bash",
            tool_input={"command": "bazel build //..."},
        )
        result = await _post_hook(client, HookRequest(hook=hook_input, env=env))
        assert result.output is not None

    async def test_post_tool_use_non_file_tool_returns_empty(self, client: AsyncClient, env: dict[str, str]) -> None:
        """PostToolUse for non-file-modifying tool returns no output."""
        hook_input = PostToolUseInput(
            **_COMMON,
            hook_event_name="PostToolUse",
            tool_use_id="toolu_test",
            tool_name="Bash",
            tool_input={"command": "echo hi"},
            tool_response="",
        )
        req = HookRequest(hook=hook_input, env=env)
        resp = await client.post("/hook", content=req.model_dump_json(), headers=_JSON_HEADERS)
        assert resp.status_code == 200
        # Response should be empty or have only default fields (no blocking decision)
        body = resp.json()
        output = body.get("output")
        assert output is None or output.get("decision") is None

    async def test_unhandled_hook_type_returns_empty(self, client: AsyncClient, env: dict[str, str]) -> None:
        """Unhandled hook types (e.g. Stop) return no output — just logged."""
        hook_input = StopInput(**_COMMON, hook_event_name="Stop", stop_hook_active=True, last_assistant_message="test")
        result = await _post_hook(client, HookRequest(hook=hook_input, env=env))
        assert result.output is None

    async def test_stop_without_last_assistant_message(self, client: AsyncClient, env: dict[str, str]) -> None:
        """Stop hook works when last_assistant_message is absent."""
        hook_input = StopInput(**_COMMON, hook_event_name="Stop", stop_hook_active=False)
        result = await _post_hook(client, HookRequest(hook=hook_input, env=env))
        assert result.output is None

    async def test_env_persisted_to_disk(self, client: AsyncClient, tmp_path: Path) -> None:
        """Session env is written to disk on each request."""
        env = {"MY_VAR": "my_value", "PATH": "/usr/bin"}
        hook_input = StopInput(**_COMMON, hook_event_name="Stop", stop_hook_active=True, last_assistant_message="test")
        await _post_hook(client, HookRequest(hook=hook_input, env=env))

        env_file = tmp_path / "hook-daemon" / "session_env.json"
        assert env_file.exists()
        saved = json.loads(env_file.read_text())
        assert saved["MY_VAR"] == "my_value"

    async def test_response_excludes_none_fields(self, client: AsyncClient, env: dict[str, str]) -> None:
        """Response JSON omits None-valued fields for forward compatibility.

        The daemon may run newer code than the client (e.g. daemon from source
        tree, client from Nix package). CamelModel uses extra="forbid", so any
        new Optional field serialized as null breaks older clients with a
        ValidationError. Verify that /hook responses never contain null values.
        """
        hook_input = PreToolUseInput(
            **_COMMON,
            hook_event_name="PreToolUse",
            tool_use_id="toolu_test",
            tool_name="Bash",
            tool_input={"command": "ls"},
        )
        req = HookRequest(hook=hook_input, env=env)
        resp = await client.post("/hook", content=req.model_dump_json(), headers=_JSON_HEADERS)
        assert resp.status_code == 200

        def _assert_no_nulls(obj: object, path: str = "") -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    assert v is not None, f"Null value at {path}.{k} — use exclude_none=True"
                    _assert_no_nulls(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    _assert_no_nulls(v, f"{path}[{i}]")

        _assert_no_nulls(resp.json())


class TestHealth:
    async def test_health_returns_ok(self, client: AsyncClient) -> None:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


def _shim_request(shim: str, argv: list[str], env: dict[str, str] | None = None) -> ShimExecRequest:
    return ShimExecRequest(
        shim=shim,
        session_id="test-session",
        cwd="/tmp",
        argv=argv,
        pid=0,
        env=env or {"HOME": "/tmp", "PATH": "/usr/bin"},
    )


class TestShimExec:
    async def _post_shim(self, client: AsyncClient, req: ShimExecRequest) -> ShimBlocked | ShimExecve:
        resp = await client.post("/shim-exec", content=req.model_dump_json(), headers=_JSON_HEADERS)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        return TypeAdapter(ShimBlocked | ShimExecve).validate_json(resp.content)

    @pytest.mark.parametrize(
        "argv",
        [
            ["git", "add", "-A"],
            ["git", "add", "--all"],
            ["git", "add", "."],
            ["git", "add", "-Av"],
            ["git", "stash"],
            ["git", "stash", "push"],
            ["git", "commit", "--amend"],
        ],
    )
    async def test_git_blocked_commands(self, client: AsyncClient, argv: list[str]) -> None:
        result = await self._post_shim(client, _shim_request("git", argv))
        assert isinstance(result, ShimBlocked)

    @pytest.mark.parametrize(
        "argv",
        [
            ["git", "add", "file.py"],
            ["git", "commit", "-m", "msg"],
            ["git", "status"],
            ["git", "stash", "list"],
            ["git", "stash", "show"],
        ],
    )
    async def test_git_allowed_commands(self, client: AsyncClient, argv: list[str]) -> None:
        result = await self._post_shim(client, _shim_request("git", argv))
        assert isinstance(result, ShimExecve)
        assert result.argv == argv

    async def test_bazelisk_injects_bazelrc(self, client: AsyncClient, tmp_path: Path) -> None:
        """Bazelisk shim response has --bazelrc injected into argv."""
        env = {
            "HOME": str(tmp_path),
            "PATH": "/usr/bin",
            "DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR": str(tmp_path / "session"),
        }
        result = await self._post_shim(client, _shim_request("bazelisk", ["bazelisk", "build", "//..."], env))
        assert isinstance(result, ShimExecve)
        assert any(a.startswith("--bazelrc=") for a in result.argv), f"Expected --bazelrc in {result.argv}"
        assert result.argv[0] == "bazelisk"
        assert "build" in result.argv
        assert "//..." in result.argv

    async def test_unknown_shim_passthrough(self, client: AsyncClient) -> None:
        """Unknown shim names pass argv through unchanged."""
        argv = ["something", "--flag", "arg"]
        result = await self._post_shim(client, _shim_request("something", argv))
        assert isinstance(result, ShimExecve)
        assert result.argv == argv

    async def test_proxy_creds_updated(self, client: AsyncClient, tmp_path: Path) -> None:
        """Shim-exec updates proxy creds from env's HTTPS_PROXY."""
        # First create a session via a hook request so app.state.sessions is populated
        hook_input = StopInput(**_COMMON, hook_event_name="Stop", stop_hook_active=False)
        env = {"HOME": "/tmp", "PATH": "/usr/bin", "CLAUDE_ENV_FILE": str(tmp_path / "e.sh")}
        await _post_hook(client, HookRequest(hook=hook_input, env=env))

        # Now send shim-exec with HTTPS_PROXY — should not error
        proxy_env = {**env, "HTTPS_PROXY": "http://user:pass@proxy:8080"}
        result = await self._post_shim(client, _shim_request("bazelisk", ["bazelisk", "build"], proxy_env))
        assert isinstance(result, ShimExecve)


class TestGitShimConfigDisabled:
    """When git_shim blocks are disabled (web mode), all git commands pass through."""

    @pytest.fixture
    async def permissive_client(self, tmp_path: Path) -> AsyncGenerator[AsyncClient]:
        daemon_dir = tmp_path / "hook-daemon"
        daemon_dir.mkdir()
        app = create_app(daemon_dir, profile=_PERMISSIVE_PROFILE, startup=StartupResult())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    async def _post_shim(self, client: AsyncClient, req: ShimExecRequest) -> ShimBlocked | ShimExecve:
        resp = await client.post("/shim-exec", content=req.model_dump_json(), headers=_JSON_HEADERS)
        assert resp.status_code == 200
        return TypeAdapter(ShimBlocked | ShimExecve).validate_json(resp.content)

    @pytest.mark.parametrize(
        "argv",
        [
            ["git", "add", "-A"],
            ["git", "add", "--all"],
            ["git", "add", "."],
            ["git", "stash"],
            ["git", "stash", "push"],
            ["git", "commit", "--amend"],
        ],
    )
    async def test_git_commands_allowed_when_disabled(self, permissive_client: AsyncClient, argv: list[str]) -> None:
        result = await self._post_shim(permissive_client, _shim_request("git", argv))
        assert isinstance(result, ShimExecve)
        assert result.argv == argv


if __name__ == "__main__":
    pytest_bazel.main()
