"""The checked-in MCP tool definitions and vectors are what this service's tools list and answer, so the Go server
they pin is held to them."""

import asyncio
import json

import pytest_bazel

from agentplane.action_service.generate_mcp_golden import build, serialize, tool_definitions
from util.bazel.runfiles import get_required_path

PACKAGE = "_main/agentplane/mcp_edge/mcpserver"
REGENERATE = 'regenerate with: bb run //agentplane/action_service:generate_mcp_golden_bin -- "$PWD/agentplane/mcp_edge/mcpserver"'


def test_tool_definitions_are_what_fastmcp_lists() -> None:
    checked_in = get_required_path(f"{PACKAGE}/tools.json").read_text(encoding="utf-8")
    assert checked_in == serialize(asyncio.run(tool_definitions())), REGENERATE


def test_golden_vectors_are_what_the_python_tools_answer() -> None:
    checked_in = get_required_path(f"{PACKAGE}/golden.json").read_text(encoding="utf-8")
    produced = serialize(asyncio.run(build()))
    # Section by section first, so a failure names the one that changed.
    for section, stored in json.loads(checked_in).items():
        assert stored == json.loads(produced)[section], section
    assert checked_in == produced, REGENERATE


if __name__ == "__main__":
    pytest_bazel.main()
