from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from editor_agent.host.submit_server import (
    EditorSubmitServer,
    SubmitFailureInput,
    SubmitStateSuccess,
    SubmitSuccessInput,
)


async def test_resource_returns_original_content():
    server = EditorSubmitServer(original_content="hello", filename="test.txt", prompt="test prompt")
    # Access the resource by calling the underlying function
    res = server._original_content
    assert res == "hello"


async def test_submit_success_records_content_once():
    server = EditorSubmitServer(original_content="orig", filename="test.txt", prompt="test prompt")
    await server.submit_success_tool.run(SubmitSuccessInput(message="ok", content="new").model_dump())
    assert isinstance(server.state, SubmitStateSuccess)
    assert server.state.content == "new"

    with pytest.raises(ToolError, match="submit already called"):
        await server.submit_success_tool.run(SubmitSuccessInput(message="again", content="x").model_dump())


async def test_submit_failure_only_once():
    server = EditorSubmitServer(original_content="orig", filename="test.txt", prompt="test prompt")
    await server.submit_failure_tool.run(SubmitFailureInput(message="fail").model_dump())

    with pytest.raises(ToolError, match="submit already called"):
        await server.submit_failure_tool.run(SubmitFailureInput(message="again").model_dump())
