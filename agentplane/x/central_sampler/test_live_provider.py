"""A real central-sampler agent against the live model provider (LiteLLM), with real Postgres, V8 host and executor.

Run explicitly; it is `manual` and needs the BuildBuddy `OPENAI_API_KEY` secret:
`bbr test //agentplane/x/central_sampler:test_live_provider.live --config=live-openai
--test_tag_filters=live_openai_api`.
"""

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_bazel
from pydantic import SecretStr

from agentplane.proto import event_pb2
from agentplane.runner import runner_service_pb2
from agentplane.x.central_sampler import sampler_pb2, sampler_pb2_grpc
from agentplane.x.central_sampler.settings import AuthConfig, ConnectionConfig, ModelConfig, ResponsesConfig
from agentplane.x.central_sampler.testing import METADATA, open_request, service, settings
from agentplane.x.central_sampler.testing_store import IntentSnapshot, Store
from util.testing.sqlalchemy_engines import postgres_async_engine
from util.testing.undeclared_outputs import undeclared_outputs_dir

# A live turn samples the model several times and runs a nested command; generous but bounded.
TURN_TIMEOUT_SECONDS = 240
EFFORT = "low"


@dataclass
class Turn:
    completed: event_pb2.TurnCompleted
    assistant_text: list[str] = field(default_factory=list)


async def follow_turn(stub: sampler_pb2_grpc.SamplerAsyncStub, session_id: str, turn_id: str) -> Turn:
    texts: dict[str, str] = {}
    assistant_items: set[str] = set()
    stream = stub.FollowEvents(sampler_pb2.FollowEventsRequest(session_id=session_id, follow=True), metadata=METADATA)
    try:
        async with asyncio.timeout(TURN_TIMEOUT_SECONDS + 30):
            async for frame in stream:
                event = frame.entry.event
                if event.HasField("item_started") and event.item_started.kind == event_pb2.ITEM_KIND_ASSISTANT_TEXT:
                    assistant_items.add(event.item_started.item_id)
                elif event.HasField("item_completed") and event.item_completed.HasField("text"):
                    texts[event.item_completed.item_id] = event.item_completed.text
                elif event.HasField("turn_completed") and event.turn_completed.turn_id == turn_id:
                    return Turn(
                        completed=event.turn_completed,
                        assistant_text=[text for item, text in texts.items() if item in assistant_items],
                    )
    finally:
        stream.cancel()
    raise AssertionError("event stream ended before the turn completed")


async def submit(stub: sampler_pb2_grpc.SamplerAsyncStub, session_id: str, command_id: str, text: str) -> Turn:
    await stub.SubmitInput(
        runner_service_pb2.SubmitInputRequest(session_id=session_id, command_id=command_id, text=text),
        metadata=METADATA,
        timeout=10,
    )
    return await follow_turn(stub, session_id, command_id)


@pytest.mark.live_openai_api
async def test_agent_runs_a_nested_command_and_continues(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    model = os.environ["OPENAI_MODEL"]
    base = settings(
        database_url, "https://unused.invalid", execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
    )
    config = base.model_copy(
        update={
            "models": {
                model: ModelConfig(
                    connection="provider",
                    provider_model=model,
                    reasoning_efforts={EFFORT},
                    api="responses",
                    responses=ResponsesConfig(dialect="lite"),
                    exec_encoding="freeform",
                )
            },
            "connections": {
                "provider": ConnectionConfig(
                    base_url=os.environ["OPENAI_BASE_URL"].rstrip("/").removesuffix("/v1"),
                    auth=AuthConfig(type="bearer", credential=SecretStr(os.environ["OPENAI_API_KEY"])),
                )
            },
            "turn_timeout_seconds": TURN_TIMEOUT_SECONDS,
        }
    )
    token = f"sampler-live-{uuid4().hex}"
    request = open_request(tmp_path)
    request.spec.model = model
    request.spec.reasoning_effort = EFFORT
    async with service(config) as stub:
        await stub.OpenSession(request, metadata=METADATA, timeout=10)
        first = await submit(
            stub,
            request.session_id,
            "first",
            f"Use exec_command to run the shell command `echo {token}`, then tell me exactly what it printed.",
        )
        second = await submit(
            stub, request.session_id, "second", "What exact token did the command print? Answer with only the token."
        )
        session = await sampler_session(request.session_id, database_url)

    assert first.completed.status == event_pb2.TURN_STATUS_COMPLETED, first.completed.error
    assert second.completed.status == event_pb2.TURN_STATUS_COMPLETED, second.completed.error
    assert not session.blocked
    nested = [
        intent
        for intent in session.intents
        if intent.request.get("kind") == "tool" and intent.request.get("tool_name", {}).get("name") == "exec_command"
    ]
    assert any(token in json.dumps(intent.result) for intent in nested), [intent.request for intent in nested]
    # The model's own answers: the first reports the command output, the second recalls it from the
    # continued conversation (encrypted reasoning, `reasoning.context=all_turns`).
    assert any(token in text for text in first.assistant_text), first.assistant_text
    assert any(token in text for text in second.assistant_text), second.assistant_text


@dataclass(frozen=True)
class SessionState:
    blocked: bool
    intents: list[IntentSnapshot]


async def sampler_session(session_id: str, database_url: str) -> SessionState:
    async with postgres_async_engine(database_url) as engine:
        store = Store(engine)
        return SessionState(blocked=(await store.get_session(session_id)).blocked, intents=await store.intents())


if __name__ == "__main__":
    pytest_bazel.main()
