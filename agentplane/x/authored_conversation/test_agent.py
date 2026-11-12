import asyncio
import json

import httpx2
import pytest
import pytest_bazel
from openai import APIStatusError

from agentplane.x.authored_conversation.agent import Status
from agentplane.x.authored_conversation.settings import Mode, Settings
from agentplane.x.authored_conversation.store import Author, Store
from agentplane.x.authored_conversation.trace import read_trace


def response(output, *, status="completed") -> httpx2.Response:
    return httpx2.Response(
        200, json={"id": "resp_test", "object": "response", "created_at": 1, "status": status, "output": output}
    )


def call(name: str, arguments, *, call_id="call_test", status="completed"):
    return {
        "type": "function_call",
        "id": "fc_" + call_id,
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments),
        "status": status,
    }


def text(body: str):
    return {
        "type": "message",
        "id": "msg_test",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": body, "annotations": []}],
    }


async def test_thread_is_explicit_and_private_output_stays_in_trace(
    make_agent, published, settings: Settings, store: Store
):
    requests = []
    replies = iter(
        [
            [
                {"type": "reasoning", "id": "rs_test", "summary": [], "encrypted_content": "opaque-test-value"},
                call("read_thread", {}),
            ],
            [call("post_message", {"body": "Published answer"}, call_id="post")],
            [text("Private diagnostic answer")],
        ]
    )

    def handler(request):
        assert request.url.path == "/v1/responses"
        body = json.loads(request.content)
        assert body["stream"] is False
        assert body["tool_choice"] == "auto"
        requests.append(body)
        return response(next(replies))

    agent = await make_agent(handler)
    human = agent.submit("Only a thread read should expose this input")
    result = await agent.run()
    assert result.status is Status.COMPLETED
    assert human.body not in json.dumps(requests[0]["input"])
    assert str(human.id) in json.dumps(requests[0]["input"])
    assert human.body in requests[1]["input"][-1]["output"]
    reasoning = next(item for item in requests[1]["input"] if item.get("type") == "reasoning")
    assert reasoning["encrypted_content"] == "opaque-test-value"
    assert [message.body for message in published] == ["Published answer"]
    assert [message.body for message in store.messages()] == [human.body, "Published answer"]
    events = read_trace(settings.state_directory / "trace.jsonl")
    assert any(
        event.kind == "model_response" and "Private diagnostic answer" in event.model_dump_json() for event in events
    )


async def test_missing_publication_is_a_failure_without_promoting_text(make_agent, published, store: Store):
    agent = await make_agent(lambda request: response([text("I forgot to publish")]))
    agent.submit("Answer me")
    result = await agent.run()
    assert result.status is Status.MISSING_PUBLICATION
    assert published == []
    assert store.messages(author=Author.AGENT) == []


async def test_input_arriving_during_final_response_is_not_stranded(make_agent, published):
    sampling = asyncio.Event()
    release = asyncio.Event()
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            sampling.set()
            await release.wait()
            return response([text("Old private answer")])
        if len(requests) == 2:
            return response([call("read_thread", {})])
        if len(requests) == 3:
            return response([call("post_message", {"body": "Correction applied"}, call_id="corrected")])
        return response([])

    agent = await make_agent(handler)
    agent.submit("Original request")
    run = asyncio.create_task(agent.run())
    await sampling.wait()
    correction = agent.submit("Correction before you finish")
    with pytest.raises(ValueError, match="wait for the active run"):
        agent.reset_context()
    with pytest.raises(ValueError, match="a run is already active"):
        await agent.run()
    release.set()
    result = await run
    assert result.status is Status.COMPLETED
    assert str(correction.id) in json.dumps(requests[1]["input"])
    assert correction.body in requests[2]["input"][-1]["output"]
    assert [message.body for message in published] == ["Correction applied"]
    assert not agent.has_pending_input()


async def test_reset_preserves_conversation_without_replaying_command(make_agent, settings: Settings, store: Store):
    requests = []
    replies = iter(
        [
            [call("run_command", {"command": "printf x >> counter"})],
            [call("post_message", {"body": "Decision: keep the result; next discuss naming"}, call_id="post")],
            [text("private execution detail")],
            [call("read_thread", {}, call_id="reread")],
            [call("post_message", {"body": "Next: discuss naming"}, call_id="next")],
            [],
        ]
    )

    def handler(request):
        requests.append(json.loads(request.content))
        return response(next(replies))

    agent = await make_agent(handler)
    agent.submit("Do the work, then discuss naming")
    assert (await agent.run()).status is Status.COMPLETED
    retained = store.messages()
    agent.reset_context()
    assert store.messages() == retained
    agent.submit("What is next?")
    assert (await agent.run()).status is Status.COMPLETED
    assert "private execution detail" not in json.dumps(requests[3]["input"])
    assert "run_command" not in json.dumps(requests[3]["input"])
    assert (settings.workspace / "counter").read_text() == "x"


@pytest.mark.parametrize("case", ["response_incomplete", "call_incomplete", "duplicate_calls"])
async def test_incomplete_or_conflicting_calls_never_execute(make_agent, settings: Settings, case):
    effect = call("run_command", {"command": "touch executed"})
    status = "completed"
    output = [effect]
    if case == "response_incomplete":
        status = "incomplete"
    elif case == "call_incomplete":
        effect["status"] = "in_progress"
    else:
        output.append(effect)
    agent = await make_agent(lambda request: response(output, status=status))
    agent.submit("Perform work")
    assert (await agent.run()).status is Status.INCOMPLETE_RESPONSE
    assert not (settings.workspace / "executed").exists()


async def test_step_limit_stops_tool_loop(make_agent, settings: Settings):
    settings.max_steps = 2
    agent = await make_agent(lambda request: response([call("read_thread", {})]))
    agent.submit("Work")
    result = await agent.run()
    assert result.status is Status.STEP_LIMIT
    assert result.steps == 2


async def test_provider_failure_is_not_retried_or_replayed(make_agent, settings: Settings):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return response([call("run_command", {"command": "printf x >> counter"})])
        return httpx2.Response(503, json={"error": {"message": "provider unavailable", "type": "test_error"}})

    agent = await make_agent(handler)
    agent.submit("Perform work")
    with pytest.raises(APIStatusError) as exc:
        await agent.run()
    assert exc.value.status_code == 503
    assert len(requests) == 2
    assert (settings.workspace / "counter").read_text() == "x"
    assert not agent.has_pending_input()


async def test_chat_control_uses_direct_replies_and_rebuilds_visible_context(make_agent, settings: Settings, published):
    settings.mode = Mode.CHAT
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert {tool["name"] for tool in body["tools"]} == {"read_file", "run_command"}
        return response([text("Direct reply")])

    agent = await make_agent(handler)
    agent.submit("Direct input")
    assert (await agent.run()).status is Status.COMPLETED
    assert requests[0]["input"] == [{"role": "user", "content": "Direct input"}]
    assert [message.body for message in published] == ["Direct reply"]
    agent.reset_context()
    assert agent.context == [
        {"role": "user", "content": "Direct input"},
        {"role": "assistant", "content": "Direct reply"},
    ]


if __name__ == "__main__":
    pytest_bazel.main()
