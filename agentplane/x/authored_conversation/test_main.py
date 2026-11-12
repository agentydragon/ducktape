import asyncio
import json

import httpx2
import pytest_bazel

from agentplane.x.authored_conversation.main import interact
from agentplane.x.authored_conversation.settings import Settings
from agentplane.x.authored_conversation.store import Author, Store


async def test_cli_wait_barrier_eof_and_separate_trace(make_agent, settings: Settings, store: Store, capsys):
    responses = iter(
        [
            [
                {
                    "type": "function_call",
                    "id": "fc_post",
                    "call_id": "post",
                    "name": "post_message",
                    "arguments": '{"body":"Visible answer"}',
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
    reader = asyncio.StreamReader()
    reader.feed_data(b"Question\n/wait\n/thread\n/reset-context\n")
    reader.feed_eof()
    await interact(agent, reader, settings.state_directory / "trace.jsonl")
    assert [message.author for message in store.messages()] == [Author.HUMAN, Author.AGENT]
    output = capsys.readouterr().out
    assert "Visible answer" in output
    assert "Private detail" not in output
    assert len(requests) == 2
    assert agent.context == []


if __name__ == "__main__":
    pytest_bazel.main()
