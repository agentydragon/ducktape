import asyncio
import json
import os
import subprocess
import sys

import httpx2
import pytest
import pytest_bazel

from agentplane.x.authored_conversation.main import display, edit_input, interact
from agentplane.x.authored_conversation.settings import Settings
from agentplane.x.authored_conversation.store import Author, Store
from agentplane.x.authored_conversation.trace import Trace


async def test_cli_wait_barrier_eof_and_separate_trace(make_agent, settings: Settings, store: Store, capsys):
    body = "# Visible answer\n\n**Strong** text.\n\n```python\nprint(42)\n```"
    responses = iter(
        [
            [
                {
                    "type": "function_call",
                    "id": "fc_post",
                    "call_id": "post",
                    "name": "post_message",
                    "arguments": json.dumps({"body": body}),
                    "status": "completed",
                }
            ],
            [
                {
                    "type": "message",
                    "id": "msg_private",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "Private detail", "annotations": []}],
                }
            ],
        ]
    )
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "resp_cli",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "output": next(responses),
            },
        )

    agent = await make_agent(handler)
    agent.display = display
    reader = asyncio.StreamReader()
    reader.feed_data(b"**Question**\n/wait\n/thread\n/reset-context\n")
    reader.feed_eof()
    await interact(agent, reader, settings.state_directory / "trace.jsonl")
    assert [message.author for message in store.messages()] == [Author.HUMAN, Author.AGENT]
    output = capsys.readouterr().out
    assert "Visible answer" in output
    assert "Strong" in output
    assert "print(42)" in output
    assert "**Question**" not in output
    assert "**Strong**" not in output
    assert "```" not in output
    assert "\x1b[" not in output
    assert store.messages()[0].body == "**Question**"
    assert store.messages()[1].body == body
    assert "Private detail" not in output
    assert len(requests) == 2
    assert agent.context == []


async def test_scripted_edit_and_revision_view(make_agent, settings: Settings, store: Store, capsys):
    original = store.append(Author.HUMAN, "Original request")
    store.append(Author.AGENT, "Existing answer")
    agent = await make_agent(
        lambda request: httpx2.Response(
            200, json={"id": "resp_edit", "object": "response", "created_at": 1, "status": "completed", "output": []}
        )
    )
    reader = asyncio.StreamReader()
    reader.feed_data(f"/edit {original.id} 1 Revised request\n/wait\n/revisions {original.id}\n".encode())
    reader.feed_eof()
    await interact(agent, reader, settings.state_directory / "trace.jsonl")
    output = capsys.readouterr().out
    assert "r2; edited" in output
    assert "Original request" in output
    assert "Revised request" in output
    assert store.get(original.id).body == "Revised request"
    assert len(store.messages()) == 2
    with pytest.raises(ValueError, match="expected-revision"):
        edit_input(agent, f"/edit {original.id}")


def test_large_piped_trace_remains_complete(settings: Settings):
    settings.state_directory.mkdir()
    config = settings.state_directory / "config.json"
    config.write_text(settings.model_dump_json())
    payload = "x" * 1024 * 1024
    with (settings.state_directory / "trace.jsonl").open("w") as stream:
        Trace(stream).record("large_test_event", data={"payload": payload})
    result = subprocess.run(
        [sys.executable, "-m", "agentplane.x.authored_conversation.main", "--config", str(config)],
        input=b"/trace\n/quit\n",
        capture_output=True,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(sys.path),
            "PYTHONDONTWRITEBYTECODE": "1",
            "EXPERIMENT_TEST_KEY": "test-credential",
        },
        timeout=20,
        check=True,
    )
    assert payload.encode() in result.stdout


if __name__ == "__main__":
    pytest_bazel.main()
