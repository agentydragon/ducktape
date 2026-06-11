from __future__ import annotations

from importlib import resources

import pytest_bazel
from mcp import types

from mcp_infra.compositor.rendering import render_compositor_instructions
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.snapshots import RunningServerEntry


def test_template_packaged() -> None:
    # Ensure the template is available via importlib.resources
    pkg = "mcp_infra.compositor.templates"
    text = resources.files(pkg).joinpath("compositor_instructions.md.j2").read_text("utf-8")
    assert "Instructions" in text


def test_render_empty_states_returns_empty() -> None:
    out = render_compositor_instructions({})
    assert out == ""


def test_render_single_running_with_instructions() -> None:
    init = types.InitializeResult(
        protocolVersion="1.0",
        capabilities=types.ServerCapabilities(),
        serverInfo=types.Implementation(name="docker_exec", version="0.0.0"),
        instructions="Hello world",
    )
    state = RunningServerEntry(initialize=init, tools=[])
    out = render_compositor_instructions({MCPMountPrefix("docker_exec"): state})
    assert "The following MCP servers" in out
    assert "# docker_exec" in out
    assert "## Instructions" in out
    assert "Hello world" in out


if __name__ == "__main__":
    pytest_bazel.main()
