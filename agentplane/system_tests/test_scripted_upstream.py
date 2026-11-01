import asyncio
import errno
import json
import socket

import httpx
import pytest
import pytest_bazel

from agentplane.native.claude.testing.requests import Message, MessagesRequest, ThinkingConfig
from agentplane.proto import runner_pb2
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from agentplane.testing.scripted_model import Text
from util.net import listen_tcp

# runner_pb2.pyi imports protobuf stubs directly during mypy traversal.
# gazelle:include_dep @pypi//protobuf


async def test_scenarios_reuse_the_session_listener_without_rebinding() -> None:
    with listen_tcp() as listener:
        for _ in range(2):
            upstream = ScriptedUpstream()
            async with upstream.serving(listener=listener.socket), httpx.AsyncClient() as client:
                response = await client.get(f"http://{listener.host}:{listener.port}/missing")
                assert response.status_code == 404
            with socket.socket() as competitor, pytest.raises(OSError, match="Address already in use") as failure:
                competitor.bind((listener.host, listener.port))
            assert failure.value.errno == errno.EADDRINUSE


async def test_case_scopes_isolate_unanswered_requests_and_release_their_queues() -> None:
    upstream = ScriptedUpstream()
    with listen_tcp() as listener:
        async with asyncio.timeout(5), upstream.serving(listener=listener.socket), httpx.AsyncClient() as client:

            async def exchange(pod: str, *, answer: bool) -> None:
                async with upstream.case_scope():
                    model = upstream.model(pod, runner_pb2.HARNESS_CLAUDE)
                    async with asyncio.TaskGroup() as requests:
                        posting = requests.create_task(
                            client.post(
                                f"http://{listener.host}:{listener.port}/v1/messages",
                                headers={"x-litellm-spend-logs-metadata": json.dumps({"agentplane.pod_name": pod})},
                                json=MessagesRequest(
                                    model="test",
                                    stream=False,
                                    system="",
                                    messages=[Message(role="user", content="hello")],
                                    thinking=ThinkingConfig(type="disabled"),
                                ).model_dump(),
                            )
                        )
                        request = await model.request()
                        if answer:
                            await model.reply(request, Text("hello"))
                            assert (await posting).status_code == 200
                        else:
                            posting.cancel()

            async def bad() -> None:
                with pytest.raises(ExceptionGroup) as failure:
                    await exchange("bad", answer=False)
                assert failure.value.subgroup(AssertionError) is not None

            async with asyncio.TaskGroup() as cases:
                cases.create_task(bad())
                cases.create_task(exchange("good", answer=True))
            await exchange("bad", answer=True)


if __name__ == "__main__":
    pytest_bazel.main()
