import asyncio
import errno
import json
import socket

import httpx
import pytest

from agentplane.native.claude.testing.requests import Message, MessagesRequest, ThinkingConfig
from agentplane.native.codex.testing.requests import ClientMetadata, InputMessage, ResponsesRequest
from agentplane.proto import session_pb2
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.testing.scripted_model import Text
from util.net import listen_tcp

# session_pb2.pyi imports protobuf stubs directly during mypy traversal.
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


@pytest.mark.parametrize("central", [False, True])
async def test_case_scopes_isolate_unanswered_requests_and_release_their_queues(central: bool) -> None:
    upstream = ScriptedUpstream()
    with listen_tcp() as listener:
        async with asyncio.timeout(5), upstream.serving(listener=listener.socket), httpx.AsyncClient() as client:

            async def exchange(pod: str, *, answer: bool) -> None:
                async with upstream.case_scope():
                    if central:
                        model = upstream.session_model(pod, session_pb2.HARNESS_CLAUDE)
                        attribution = {"agentplane.session_id": pod, "agentplane.identity": "test-identity"}
                    else:
                        model = upstream.model(pod, session_pb2.HARNESS_CLAUDE)
                        attribution = {"agentplane.pod_name": pod}
                    async with asyncio.TaskGroup() as requests:
                        posting = requests.create_task(
                            client.post(
                                f"http://{listener.host}:{listener.port}/v1/messages",
                                headers={"x-litellm-spend-logs-metadata": json.dumps(attribution)},
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


async def test_responses_route_central_sessions_separately_from_each_other_and_pods() -> None:
    upstream = ScriptedUpstream()
    with listen_tcp() as listener:
        async with (
            asyncio.timeout(5),
            upstream.serving(listener=listener.socket),
            upstream.case_scope(),
            httpx.AsyncClient() as client,
        ):

            async def exchange(name: str, *, central: bool) -> None:
                if central:
                    model = upstream.session_model(name, session_pb2.HARNESS_CODEX)
                    attribution = {
                        "agentplane.session_id": name,
                        "agentplane.identity": "test-shared-owner",
                        "agentplane.identity_uid": "test-shared-owner-uid",
                    }
                else:
                    model = upstream.model(name, session_pb2.HARNESS_CODEX)
                    attribution = {"agentplane.pod_name": name}
                prompt = f"{'session' if central else 'pod'}-{name}"
                async with asyncio.TaskGroup() as requests:
                    posting = requests.create_task(
                        client.post(
                            f"http://{listener.host}:{listener.port}/v1/responses",
                            headers={"x-litellm-spend-logs-metadata": json.dumps(attribution)},
                            json=ResponsesRequest(
                                model="test",
                                stream=True,
                                input=[InputMessage(type="message", role="user", content=prompt)],
                                prompt_cache_key="test-cache",
                                client_metadata=ClientMetadata(
                                    session_id=name, thread_id="test-thread", turn_id="test-turn"
                                ),
                            ).model_dump(),
                        )
                    )
                    request = await model.request()
                    assert request.user_texts == [prompt]
                    await model.reply(request, Text(f"reply-{prompt}"))
                    response = await posting
                    assert response.status_code == 200
                    assert f"reply-{prompt}" in response.text

            async with asyncio.TaskGroup() as exchanges:
                exchanges.create_task(exchange("test-a", central=True))
                exchanges.create_task(exchange("test-b", central=True))
                exchanges.create_task(exchange("test-a", central=False))
