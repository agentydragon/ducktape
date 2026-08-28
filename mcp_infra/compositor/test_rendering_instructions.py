from __future__ import annotations

import pytest_bazel
from mcp import types

from mcp_infra.compositor.rendering import render_compositor_instructions
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.snapshots import RunningServerEntry


def test_render_single_running_with_instructions() -> None:
    """The mount prefix becomes a heading and the server's instructions surface in the output."""
    init = types.InitializeResult(
        protocolVersion="1.0",
        capabilities=types.ServerCapabilities(),
        serverInfo=types.Implementation(name="docker_exec", version="0.0.0"),
        instructions="Hello world",
    )
    state = RunningServerEntry(initialize=init, tools=[])
    out = render_compositor_instructions({MCPMountPrefix("docker_exec"): state})
    assert "# docker_exec" in out
    assert "Hello world" in out


if __name__ == "__main__":
    pytest_bazel.main()
