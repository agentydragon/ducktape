from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest_bazel
from hamcrest import assert_that

from agent_core.testing.mcp.responses import MCPDecoratorMock
from agent_core.testing.responses import PlayGen, tool_roundtrip
from editor_agent.host.agent_runner import run_editor_docker_agent
from editor_agent.host.submit_server import SubmitStateSuccess
from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.exec.matchers import exited_successfully
from mcp_infra.exec.models import BaseExecResult
from openai_utils.model import FunctionCallItem, ResponsesRequest


class HostDockerExecMock(MCPDecoratorMock):
    """Mock for host-side docker exec into containers (editor_agent pattern)."""

    def docker_exec_roundtrip(
        self, cmd: list[str], *, timeout_ms: int = 5000, cwd: str | None = None, tool_name: str = "exec"
    ) -> Generator[FunctionCallItem, ResponsesRequest, BaseExecResult]:
        """Yield MCP docker exec call (host-side docker exec into container)."""
        args: dict[str, Any] = {"cmd": cmd, "timeout_ms": timeout_ms}
        if cwd is not None:
            args["cwd"] = cwd
        call = self.mcp_tool_call(ContainerExecServer.DOCKER_MOUNT_PREFIX, tool_name, args)
        return tool_roundtrip(call, BaseExecResult)


async def test_editor_step_sequence(tmp_path, async_docker_client, editor_image_id):
    """Test editor flow: init, edit file, submit-success, and writeback to host file."""
    fname = "file.txt"
    target = tmp_path / fname
    target.write_text("hello", encoding="utf-8")

    @HostDockerExecMock.mock()
    def mock(m: HostDockerExecMock) -> PlayGen:
        yield None  # First request
        # Edit the file
        result = yield from m.docker_exec_roundtrip(["sh", "-c", f"echo 'modified content' > /workspace/{fname}"])
        assert_that(result, exited_successfully())
        # Submit success
        yield from m.docker_exec_roundtrip(
            ["editor_submit", "submit-success", "--message", "done", "--file", f"/workspace/{fname}"]
        )

    result = await run_editor_docker_agent(
        file_path=target,
        prompt="test prompt",
        docker_client=async_docker_client,
        model_client=mock,
        max_turns=10,
        image_id=editor_image_id,
    )

    # Verify success submission
    assert isinstance(result, SubmitStateSuccess)
    # Verify the modified content was written back to host
    assert target.read_text(encoding="utf-8") == "modified content\n"


if __name__ == "__main__":
    pytest_bazel.main()
