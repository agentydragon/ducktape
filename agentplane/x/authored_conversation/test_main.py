import asyncio
import errno
import json
import os
import pty
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import httpx2
import pytest_bazel

from agentplane.x.authored_conversation.main import interact
from agentplane.x.authored_conversation.settings import Settings
from agentplane.x.authored_conversation.store import Author, Store
from agentplane.x.authored_conversation.trace import Trace


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


def test_large_trace_on_shared_terminal_does_not_make_stdout_nonblocking(settings: Settings):
    settings.state_directory.mkdir()
    config = settings.state_directory / "config.json"
    config.write_text(settings.model_dump_json())
    payload = "x" * 1024 * 1024
    with (settings.state_directory / "trace.jsonl").open("w") as stream:
        Trace(stream).record("large_test_event", data={"payload": payload})

    with ExitStack() as stack:
        master_fd, slave_fd = pty.openpty()
        master = stack.enter_context(os.fdopen(master_fd, "rb", buffering=0))
        with os.fdopen(slave_fd, "rb", buffering=0) as slave:
            process = stack.enter_context(
                subprocess.Popen(
                    [sys.executable, "-m", "agentplane.x.authored_conversation.main", "--config", str(config)],
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env={
                        **os.environ,
                        "PYTHONPATH": os.pathsep.join(sys.path),
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "EXPERIMENT_TEST_KEY": "test-credential",
                    },
                )
            )

        def drain_terminal() -> bytes:
            chunks = []
            while True:
                try:
                    chunk = os.read(master.fileno(), 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break  # Linux PTYs report EIO when the last slave descriptor closes.
                    raise
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)

        with ThreadPoolExecutor(max_workers=1) as executor:
            output = executor.submit(drain_terminal)
            os.write(master.fileno(), b"/trace\n/quit\n")
            try:
                returncode = process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
            rendered = output.result(timeout=5)
        assert returncode == 0, rendered[-2000:].decode(errors="replace")
        assert payload.encode() in rendered


if __name__ == "__main__":
    pytest_bazel.main()
