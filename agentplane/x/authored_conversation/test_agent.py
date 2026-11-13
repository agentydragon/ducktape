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


async def test_explicit_yield_requires_tools_after_publication_too(make_agent, settings: Settings, published):
    settings.turn_control = "yield_tool"
    choices = []
    replies = iter(
        [
            [call("read_thread", {})],
            [call("post_message", {"body": "Published answer"}, call_id="post")],
            [call("yield_turn", {}, call_id="yield")],
        ]
    )

    def handler(request):
        body = json.loads(request.content)
        choices.append(body["tool_choice"])
        assert "yield_turn" in {tool["name"] for tool in body["tools"]}
        return response(next(replies))

    agent = await make_agent(handler)
    agent.submit("Answer me")
    assert (await agent.run()).status is Status.COMPLETED
    assert choices == ["required"] * 3
    assert [message.body for message in published] == ["Published answer"]
    assert agent.context[-1]["output"] == '{"status":"yielded"}'


async def test_plain_text_cannot_end_explicit_yield_run(make_agent, settings: Settings, published):
    settings.turn_control = "yield_tool"
    agent = await make_agent(lambda request: response([text("Not a yield call")]))
    agent.submit("Answer me")
    assert (await agent.run()).status is Status.INCOMPLETE_RESPONSE
    assert published == []


async def test_yield_without_publication_reports_missing_reply(make_agent, settings: Settings, published):
    settings.turn_control = "yield_tool"
    agent = await make_agent(lambda request: response([call("yield_turn", {})]))
    agent.submit("Answer me")
    assert (await agent.run()).status is Status.MISSING_PUBLICATION
    assert published == []


async def test_invalid_yield_arguments_do_not_end_run(make_agent, settings: Settings):
    settings.turn_control = "yield_tool"
    requests = []
    replies = iter(
        [
            [call("post_message", {"body": "Answer"}, call_id="post")],
            [call("yield_turn", {"unexpected": True}, call_id="invalid")],
            [call("yield_turn", {}, call_id="valid")],
        ]
    )

    def handler(request):
        requests.append(json.loads(request.content))
        return response(next(replies))

    agent = await make_agent(handler)
    agent.submit("Answer me")
    assert (await agent.run()).status is Status.COMPLETED
    assert len(requests) == 3
    assert "error" in json.loads(requests[2]["input"][-1]["output"])


async def test_work_after_yield_never_executes_effects(make_agent, settings: Settings):
    settings.turn_control = "yield_tool"
    agent = await make_agent(
        lambda request: response(
            [call("yield_turn", {}, call_id="yield"), call("run_command", {"command": "touch executed"})]
        )
    )
    agent.submit("Work")
    assert (await agent.run()).status is Status.INCOMPLETE_RESPONSE
    assert not (settings.workspace / "executed").exists()


async def test_publication_then_yield_in_one_batch_is_ordered(make_agent, settings: Settings, published):
    settings.turn_control = "yield_tool"
    agent = await make_agent(
        lambda request: response(
            [call("post_message", {"body": "Answer"}, call_id="post"), call("yield_turn", {}, call_id="yield")]
        )
    )
    agent.submit("Answer me")
    assert (await agent.run()).status is Status.COMPLETED
    assert [message.body for message in published] == ["Answer"]
    assert [item["call_id"] for item in agent.context if item.get("type") == "function_call_output"] == [
        "post",
        "yield",
    ]


async def test_required_tools_without_yield_still_hit_step_limit(make_agent, settings: Settings, published):
    settings.turn_control = "yield_tool"
    settings.max_steps = 2
    choices = []

    def handler(request):
        choices.append(json.loads(request.content)["tool_choice"])
        return response([call("read_thread", {}, call_id=f"read-{len(choices)}")])

    agent = await make_agent(handler)
    agent.submit("Answer me")
    assert (await agent.run()).status is Status.STEP_LIMIT
    assert choices == ["required", "required"]
    assert published == []


async def test_input_arriving_during_yield_is_not_stranded(make_agent, settings: Settings, published):
    settings.turn_control = "yield_tool"
    sampling = asyncio.Event()
    release = asyncio.Event()
    choices = []

    async def handler(request):
        choices.append(json.loads(request.content)["tool_choice"])
        match len(choices):
            case 1:
                return response([call("post_message", {"body": "First answer"}, call_id="first")])
            case 2:
                sampling.set()
                await release.wait()
                return response([call("yield_turn", {}, call_id="first-yield")])
            case 3:
                return response([call("post_message", {"body": "Correction applied"}, call_id="second")])
            case _:
                return response([call("yield_turn", {}, call_id="second-yield")])

    agent = await make_agent(handler)
    agent.submit("Original request")
    running = asyncio.create_task(agent.run())
    await sampling.wait()
    agent.submit("A correction")
    release.set()
    assert (await running).status is Status.COMPLETED
    assert choices == ["required"] * 4
    assert [message.body for message in published] == ["First answer", "Correction applied"]


def test_explicit_yield_rejects_chat_mode(settings: Settings):
    with pytest.raises(ValueError, match="requires authored mode"):
        Settings.model_validate(settings.model_dump() | {"mode": "chat", "turn_control": "yield_tool"})


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
    calls = iter(["read-1", "read-2"])
    agent = await make_agent(lambda request: response([call("read_thread", {}, call_id=next(calls))]))
    agent.submit("Work")
    result = await agent.run()
    assert result.status is Status.STEP_LIMIT
    assert result.steps == 2


async def test_duplicate_call_from_later_response_does_not_repeat_effect(make_agent, settings: Settings):
    agent = await make_agent(lambda request: response([call("run_command", {"command": "printf x >> counter"})]))
    agent.submit("Perform work")
    assert (await agent.run()).status is Status.INCOMPLETE_RESPONSE
    assert (settings.workspace / "counter").read_text() == "x"


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


@pytest.mark.parametrize("turn_control", ["implicit", "yield_tool"])
async def test_edit_during_final_sample_is_announced_without_rewriting_context_or_replaying_work(
    make_agent, settings: Settings, store: Store, published, turn_control
):
    settings.turn_control = turn_control
    sampling = asyncio.Event()
    release = asyncio.Event()
    requests = []

    def end_turn(call_id):
        return [call("yield_turn", {}, call_id=call_id)] if turn_control == "yield_tool" else []

    async def handler(request):
        requests.append(json.loads(request.content))
        match len(requests):
            case 1:
                return response([call("read_thread", {}, call_id="read")])
            case 2:
                return response([call("run_command", {"command": "printf x >> counter"}, call_id="work")])
            case 3:
                return response([call("post_message", {"body": "Original answer"}, call_id="post")])
            case 4:
                sampling.set()
                await release.wait()
                return response(end_turn("first-yield"))
            case 5:
                return response([call("read_message", {"message_id": str(human.id)}, call_id="reread")])
            case 6:
                answer = store.messages(author=Author.AGENT)[0]
                return response(
                    [
                        call(
                            "edit_message",
                            {"message_id": str(answer.id), "expected_revision": 1, "body": "Corrected answer"},
                            call_id="edit",
                        )
                    ]
                )
            case _:
                return response(end_turn("final-yield"))

    agent = await make_agent(handler)
    human = agent.submit("Original instruction")
    running = asyncio.create_task(agent.run())
    await sampling.wait()
    edited = agent.edit(human.id, expected_revision=1, body="Corrected instruction")
    assert agent.has_pending_input()
    release.set()
    assert (await running).status is Status.COMPLETED
    assert len(requests) == 7
    notice = requests[4]["input"][-1]["content"]
    assert str(human.id) in notice
    assert "revision 2 (edited)" in notice
    assert edited.body not in json.dumps(requests[4]["input"])
    assert human.body in json.dumps(requests[4]["input"])
    assert edited.body in requests[5]["input"][-1]["output"]
    assert [message.body for message in published] == ["Original answer", "Corrected answer"]
    assert len(store.messages()) == 2
    assert (settings.workspace / "counter").read_text() == "x"
    assert not agent.has_pending_input()
    agent.reset_context()
    assert agent.context == []
    reread = await agent.tools.execute("read_thread", "{}")
    assert edited.body in reread.output
    assert human.body not in reread.output
    events = read_trace(settings.state_directory / "trace.jsonl")
    assert any(event.kind == "human_message" and human.body in event.model_dump_json() for event in events)
    assert any(event.kind == "human_edit" and edited.body in event.model_dump_json() for event in events)


async def test_agent_can_answer_new_input_by_editing_an_older_message(make_agent, settings: Settings, store: Store):
    settings.turn_control = "yield_tool"
    store.append(Author.HUMAN, "First question")
    answer = store.append(Author.AGENT, "Original answer")
    agent = await make_agent(
        lambda request: response(
            [
                call(
                    "edit_message",
                    {"message_id": str(answer.id), "expected_revision": 1, "body": "Updated answer"},
                    call_id="edit",
                ),
                call("yield_turn", {}, call_id="yield"),
            ]
        )
    )
    agent.submit("Please update that answer")
    assert (await agent.run()).status is Status.COMPLETED
    assert store.get(answer.id).position == answer.position
    assert len(store.messages()) == 3


@pytest.mark.parametrize("answered", [False, True])
async def test_edit_only_may_yield_but_does_not_clear_an_unanswered_post(
    make_agent, settings: Settings, store: Store, answered
):
    settings.turn_control = "yield_tool"
    ids = iter(["yield-original", "yield-edit"])
    agent = await make_agent(lambda request: response([call("yield_turn", {}, call_id=next(ids))]))
    human = agent.submit("Question")
    if answered:
        store.append(Author.AGENT, "Answer")
    expected = Status.COMPLETED if answered else Status.MISSING_PUBLICATION
    assert (await agent.run()).status is expected
    agent.edit(human.id, expected_revision=1, body="Minor correction")
    assert (await agent.run()).status is expected


if __name__ == "__main__":
    pytest_bazel.main()
