from __future__ import annotations

from pathlib import Path

import pytest

from editor_agent.host.runner import editor_docker_session, writeback_success
from editor_agent.host.submit_server import EditorSubmitServer, SubmitStateSuccess, SubmitSuccessInput


@pytest.mark.requires_docker
async def test_editor_session_starts_and_cleans(tmp_path: Path, async_docker_client, editor_image_id):
    target = tmp_path / "file.txt"
    target.write_text("hello", encoding="utf-8")

    async with editor_docker_session(
        file_path=target, prompt="test prompt", docker_client=async_docker_client, image_id=editor_image_id
    ) as sess:
        assert sess.container_server is not None


async def test_submit_success_writes_back(tmp_path: Path):
    target = tmp_path / "file.txt"
    original = "hello world"
    target.write_text(original, encoding="utf-8")

    server = EditorSubmitServer(original_content=original, filename="file.txt", prompt="test prompt")
    await server.submit_success_tool.run(SubmitSuccessInput(message="done", content="updated").model_dump())

    assert isinstance(server.state, SubmitStateSuccess)
    writeback_success(target, server.state.content)

    assert target.read_text(encoding="utf-8") == "updated"
