import json

import pytest_bazel

from agentplane.x.authored_conversation.settings import Settings
from agentplane.x.authored_conversation.store import Author, Store
from agentplane.x.authored_conversation.tools import Tools, run_command


async def test_authorship_is_assigned_by_tool_and_invalid_posts_do_not_commit(
    settings: Settings, store: Store, published
):
    tools = Tools(settings, store, published.append)
    result = json.loads((await tools.execute("post_message", '{"body":"Answer", "author":"human"}')).output)
    assert "error" in result
    assert store.messages() == []
    await tools.execute("post_message", '{"body":"Answer"}')
    assert store.messages()[0].author is Author.AGENT
    assert published == store.messages()


async def test_file_reads_are_bounded_and_reject_symlink_escape(settings: Settings, store: Store, published, tmp_path):
    settings.max_tool_output_bytes = 4
    (settings.workspace / "inside").write_text("12345678")
    outside = tmp_path / "outside"
    outside.write_text("outside content")
    (settings.workspace / "link").symlink_to(outside)
    tools = Tools(settings, store, published.append)
    assert json.loads((await tools.execute("read_file", '{"path":"inside"}')).output) == {
        "text": "1234",
        "truncated": True,
    }
    assert "error" in json.loads((await tools.execute("read_file", '{"path":"link"}')).output)


async def test_command_output_is_bounded_and_timed_out_processes_are_reaped(settings: Settings):
    settings.max_tool_output_bytes = 64
    result = await run_command("exec yes x", settings)
    assert result.status == "output_limit"
    assert len(result.output.encode()) == 64
    assert result.exit_code != 0
    settings.command_timeout_seconds = 0.05
    result = await run_command("exec sleep 100", settings)
    assert result.status == "timeout"
    assert result.exit_code != 0


if __name__ == "__main__":
    pytest_bazel.main()
