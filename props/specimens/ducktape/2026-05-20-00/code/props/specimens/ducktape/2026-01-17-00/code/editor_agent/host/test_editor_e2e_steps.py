from __future__ import annotations

import pytest
from hamcrest import assert_that

from agent_core_testing.responses import DecoratorMock, PlayGen
from agent_core_testing.steps import exited_successfully
from editor_agent.host.agent_runner import run_editor_docker_agent
from editor_agent.host.submit_server import SubmitStateSuccess


@pytest.mark.requires_docker
async def test_editor_step_sequence(tmp_path, async_docker_client, editor_image_id):
    """Test editor flow: init, edit file, submit-success, and writeback to host file."""
    fname = "file.txt"
    target = tmp_path / fname
    target.write_text("hello", encoding="utf-8")

    @DecoratorMock.mock()
    def mock(m: DecoratorMock) -> PlayGen:
        yield None  # First request
        # Edit the file
        result = yield from m.docker_exec_roundtrip(["sh", "-c", f"echo 'modified content' > /workspace/{fname}"])
        assert_that(result, exited_successfully())
        # Submit success
        yield from m.docker_exec_roundtrip(
            ["editor-submit", "submit-success", "--message", "done", "--file", f"/workspace/{fname}"]
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
