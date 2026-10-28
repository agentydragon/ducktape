"""One unmodified local Read comparator: inspect the next model request.

Native remote registration is exercised separately by test_native_serving.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest_bazel

from agentplane.native.claude import scenarios
from agentplane.native.claude.testing import anthropic_sse
from agentplane.native.claude.testing.async_run import ClaudeRun
from agentplane.native.claude.testing.messages import AnthropicMessages
from agentplane.native.claude.testing.model import MODEL
from agentplane.native.claude.testing.requests import MessagesRequest
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

ROOT = "ROOT_INSTRUCTION_7b32c8"
NESTED = "NESTED_INSTRUCTION_5d61a4"
SIBLING = "SIBLING_INSTRUCTION_943fc1"


def _capture(name: str, request: MessagesRequest) -> str:
    text = request.model_dump_json(indent=2)
    (undeclared_outputs_dir() / f"{name}.json").write_text(text)
    return text


@asynccontextmanager
async def _run(
    workspace: Path, logs: Path, environment: dict[str, str], config: Path, endpoint: AnthropicMessages
) -> AsyncIterator[ClaudeRun]:
    await asyncio.to_thread(config.mkdir, exist_ok=True)
    await asyncio.to_thread(logs.mkdir, parents=True, exist_ok=True)
    command = scenarios.command(
        str(get_required_path("claude_remote_tools_cli_linux_x64/claude")),
        model=MODEL,
        session_name="remote-tools-characterization",
        tools=("Read", "Edit"),
        permission_mode="manual",
        # Ordinary instruction loading; hook callbacks permit explicit denial experiments.
        hooks=True,
    )
    # Enable the project source so the fixture CLAUDE.md is loaded.
    command[command.index("--setting-sources=")] = "--setting-sources=project"
    debug_file = config / f"debug-{logs.name}.log"
    command.extend(["--debug-file", str(debug_file)])
    env = {
        **environment,
        **scenarios.environment(endpoint=endpoint.origin, token="test-key", config_dir=str(config), max_retries=0),
        "CLAUDE_CODE_REMOTE_TOOLS_FORWARD": "0",
        "CLAUDE_CODE_REMOTE_TOOLS_SESSION_CHANNEL": "0",
        # Unexpected external traffic fails against this loopback rejecting destination.
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
    }
    async with ClaudeRun(
        logs, command, cwd=workspace, environment=env, responder=scenarios.hook_answers(deny_tools=False), hooks=True
    ) as run:
        try:
            yield run
        finally:
            # The CLI also creates an absolute `latest` symlink beside its debug file.
            # Publish only the real file; the temp-directory link is not a Bazel output.
            if debug_file.exists():
                (logs / "debug.log").write_text(debug_file.read_text())


def _instructions(workspace: Path) -> Path:
    (workspace / "CLAUDE.md").write_text(f"Project instruction: {ROOT}\n")
    nested = workspace / "nested"
    nested.mkdir()
    (nested / "CLAUDE.md").write_text(f"Nested instruction: {NESTED}\n")
    sibling = workspace / "sibling"
    sibling.mkdir()
    (sibling / "CLAUDE.md").write_text(f"Sibling instruction: {SIBLING}\n")
    target = nested / "editable.txt"
    target.write_text("before\n")
    return target


async def test_local_read_instruction_comparator(
    workspace: Path,
    native_logs: Path,
    base_environment: dict[str, str],
    tmp_path: Path,
    anthropic_messages: AnthropicMessages,
) -> None:
    target = _instructions(workspace)
    async with (
        asyncio.timeout(35),
        _run(workspace, native_logs, base_environment, tmp_path / "config", anthropic_messages) as run,
    ):
        prompt = await run.send("Read the nested editable file.")
        async with await anthropic_messages.await_next_request() as exchange:
            first = _capture("local-before-read", exchange.request)
            assert first.count(ROOT) == 1
            assert NESTED not in first
            assert SIBLING not in first
            assert "Read" in exchange.request.tool_names
            await exchange.send(
                *anthropic_sse.message_stream(
                    [anthropic_sse.ToolUse("native_read", "Read", {"file_path": str(target)})], model=MODEL
                ).events
            )
        async with await anthropic_messages.await_next_request() as exchange:
            after = _capture("local-after-read", exchange.request)
            assert "before" in exchange.request.tool_results[0].text
            assert after.count(ROOT) == 1
            assert after.count(NESTED) == 1
            assert SIBLING not in after
            await exchange.send(*anthropic_sse.message_stream([anthropic_sse.Text("DONE")], model=MODEL).events)
        assert (await prompt.result()).result == "DONE"


if __name__ == "__main__":
    pytest_bazel.main()
