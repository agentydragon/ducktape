"""Unit tests for hook daemon server — exercises the FastAPI app in-process."""

import json
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_bazel
from httpx import ASGITransport, AsyncClient

from devinfra.claude.claude_api.hooks.post_tool_use import PostToolUseInput
from devinfra.claude.claude_api.hooks.pre_tool_use import PreToolUseInput
from devinfra.claude.claude_api.hooks.stop import StopInput
from devinfra.claude.hook_daemon.models import HookRequest, HookResponse
from devinfra.claude.hook_daemon.server import app, configure
from devinfra.claude.hook_daemon.tracing import DeferredOtlpExporter

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
    configure(daemon_dir, DeferredOtlpExporter())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def env() -> dict[str, str]:
    return {"HOME": "/tmp", "PATH": "/usr/bin"}


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


if __name__ == "__main__":
    pytest_bazel.main()
