import json

import pytest_bazel

from agentplane.x.authored_conversation.settings import Settings
from agentplane.x.authored_conversation.store import Author, Message, Store
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


async def test_edit_tool_owns_agent_messages_and_exposes_current_and_old_revisions(
    settings: Settings, store: Store, published
):
    human = store.append(Author.HUMAN, "Request")
    original = store.append(Author.AGENT, "Draft answer")
    tools = Tools(settings, store, published.append)
    arguments = {"message_id": str(human.id), "expected_revision": 1, "body": "Revised"}
    denied = await tools.execute("edit_message", json.dumps(arguments))
    assert "own messages" in denied.output
    arguments["message_id"] = str(original.id)
    result = await tools.execute("edit_message", json.dumps(arguments))
    edited = Message.model_validate_json(result.output)
    assert edited.body == "Revised"
    assert published == [edited]
    stale = await tools.execute("edit_message", json.dumps(arguments))
    assert "revision conflict" in stale.output
    assert len(store.revisions(original.id)) == 2
    latest = await tools.execute("read_message", json.dumps({"message_id": str(original.id)}))
    past = await tools.execute("read_message", json.dumps({"message_id": str(original.id), "revision": 1}))
    assert Message.model_validate_json(latest.output) == edited
    assert Message.model_validate_json(past.output) == original


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
