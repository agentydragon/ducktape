"""Both explicit runner-control mTLS directions reach one Gateway and preserve admitted work."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.testing.client import REQUEST_SECONDS, Client
from agentplane.proto import command_pb2, event_log_pb2, event_pb2, runner_pb2
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2
from agentplane.sandbox_resource import OperatingMode
from agentplane.system_tests.agent import TURN_SECONDS, Agent, Turn
from agentplane.system_tests.cluster import kubectl
from agentplane.system_tests.conftest import FORWARD_POD_TEMPLATE, POD_TEMPLATE, Sandboxes, wait_until_running
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from agentplane.testing.scripted_model import ModelRequest, ScriptedModel, ShellCall, Text

_RUNNER_CREDENTIAL_LABEL = "agentplane.allegedly.works/runner-credential"
_RUNNER_TRANSPORT_LABEL = "agentplane.allegedly.works/runner-transport"
_SANDBOX_LABEL = "agentplane.allegedly.works/sandbox"


async def _turn(
    client: Client,
    agent: Agent,
    *,
    command_id: str,
    prompt: str,
    model: ScriptedModel[Any],
    history_turns: int = 1,
    shell_command: str | None = None,
    response: str = "Done.",
) -> tuple[Turn, list[event_log_pb2.EventEntry], ModelRequest[Any]]:
    collecting = asyncio.create_task(agent.next_turn(awaiting_command_id=command_id))
    command = command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text=prompt))
    admission = await client.submit_input(agent.thread_id, command_id, prompt)
    if isinstance(admission, session_dispatcher_pb2.Dispatch):
        assert admission.command_id == command_id
        assert admission.operation == session_dispatcher_pb2.OPERATION_SUBMIT_INPUT
    else:
        assert isinstance(admission, event_log_pb2.EventEntry)
        assert admission.event.command_admitted.command == command

    request = await model.request()
    assert prompt in request.user_texts
    if shell_command is None:
        await model.reply(request, Text(response))
    else:
        await model.reply(request, ShellCall(call_id=f"call-{uuid4().hex[:8]}", command=shell_command))
        await model.reply(await model.request(), Text(response))
    turn = await collecting
    assert turn.status == event_pb2.TURN_STATUS_COMPLETED
    entries = await _history(client, agent.thread_id, completed_turns=history_turns)
    assert any(
        entry.event.HasField("command_admitted") and entry.event.command_admitted.command.command_id == command_id
        for entry in entries
    ), f"History did not retain command admission {command_id}"
    assert (await client.thread(agent.thread_id)).last_turn_status == "TURN_STATUS_COMPLETED"
    return turn, entries, request


async def _history(client: Client, thread_id: UUID, *, completed_turns: int) -> list[event_log_pb2.EventEntry]:
    entries: list[event_log_pb2.EventEntry] = []
    observed_turns = 0
    async with asyncio.timeout(TURN_SECONDS):
        async for entry in client.events(thread_id, after=0, read_seconds=TURN_SECONDS):
            entries.append(entry)
            if entry.event.HasField("turn_completed"):
                observed_turns += 1
                if observed_turns == completed_turns:
                    break
    assert observed_turns == completed_turns, f"History ended after {observed_turns} completed turns"
    return entries


async def _identity(name: str) -> tuple[str, str, str]:
    pod = json.loads(await kubectl("get", "pod", name, "-o", "json"))
    metadata = pod["metadata"]
    labels = metadata["labels"]
    return (metadata["uid"], labels[_RUNNER_CREDENTIAL_LABEL], labels[_RUNNER_TRANSPORT_LABEL])


async def _ready_certificate(
    name: str, sandbox_name: str, sandbox_uid: str, credential_id: str, namespace: str
) -> None:
    certificate = json.loads(await kubectl("get", "certificate", name, "-o", "json"))
    spec = certificate["spec"]
    assert spec["secretName"] == f"runner-{credential_id}"
    assert spec["uris"] == [f"spiffe://agentplane/runner/{namespace}/{sandbox_uid}/{credential_id}"]
    assert set(spec["usages"]) >= {"client auth", "server auth"}
    assert any(
        condition["type"] == "Ready" and condition["status"] == "True"
        for condition in certificate["status"]["conditions"]
    )
    secret = json.loads(await kubectl("get", "secret", f"runner-{credential_id}", "-o", "json"))
    assert secret["metadata"]["annotations"]["cert-manager.io/certificate-name"] == name
    assert secret["metadata"]["labels"][_SANDBOX_LABEL] == sandbox_name
    assert secret["metadata"]["labels"][_RUNNER_CREDENTIAL_LABEL] == credential_id


async def _wait_suspended(client: Client, name: str) -> None:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(REQUEST_SECONDS),
        wait=wait_fixed(1),
        retry=retry_if_exception_type(AssertionError),
        reraise=True,
    ):
        with attempt:
            view = await client.sandbox(name)
            assert view.operating_mode is OperatingMode.SUSPENDED
            assert view.pod is None


async def _open_agent(client: Client, name: str, harness: runner_pb2.Harness, model_name: str) -> Agent:
    agent = await Agent.open(client, sandbox=name, harness=harness, model=model_name)
    await client.wait_for_thread_feed(agent.thread_id, timeout_s=REQUEST_SECONDS)
    return agent


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_forward_and_reverse_mtls_share_gateway_and_forward_resume_keeps_history(
    client: Client, sandbox: Sandboxes, harness: runner_pb2.Harness, offered_model: str, scripted: ScriptedUpstream
) -> None:
    reverse, forward = await asyncio.gather(
        sandbox(f"systest-transport-reverse-{uuid4().hex[:8]}", template=POD_TEMPLATE),
        sandbox(f"systest-transport-forward-{uuid4().hex[:8]}", template=FORWARD_POD_TEMPLATE),
    )
    assert reverse.pod is not None
    assert forward.pod is not None
    reverse_identity, forward_identity = await asyncio.gather(_identity(reverse.name), _identity(forward.name))
    assert reverse_identity[2] == "reverse"
    assert forward_identity[2] == "forward"
    await asyncio.gather(
        _ready_certificate(
            f"runner-{reverse_identity[1]}", reverse.name, str(reverse.uid), reverse_identity[1], reverse.namespace
        ),
        _ready_certificate(
            f"runner-{forward_identity[1]}", forward.name, str(forward.uid), forward_identity[1], forward.namespace
        ),
    )

    reverse_agent, forward_agent = await asyncio.gather(
        _open_agent(client, reverse.name, harness, offered_model),
        _open_agent(client, forward.name, harness, offered_model),
    )
    reverse_model = scripted.model(reverse.pod.name, harness)
    forward_model = scripted.model(forward.pod.name, harness)
    marker = f"FORWARD_CONTEXT_{uuid4().hex}"
    (reverse_turn, _, _), (forward_turn, _, _) = await asyncio.gather(
        _turn(
            client,
            reverse_agent,
            model=reverse_model,
            command_id=f"reverse-{uuid4().hex}",
            prompt="Run the reverse transport check.",
            shell_command="printf 'REVERSE_MTLS_OK\\n'",
            response="REVERSE_MTLS_OK",
        ),
        _turn(
            client,
            forward_agent,
            model=forward_model,
            command_id=f"forward-{uuid4().hex}",
            prompt=(
                f"Remember this invented non-secret code for our next turn: {marker}. Run the forward transport check."
            ),
            shell_command="printf 'FORWARD_MTLS_OK\\n'",
            response="FORWARD_MTLS_OK",
        ),
    )
    assert "REVERSE_MTLS_OK" in "\n".join(reverse_turn.tool_outputs)
    assert "FORWARD_MTLS_OK" in "\n".join(forward_turn.tool_outputs)

    old_pod_uid, old_credential_id, old_transport = await _identity(forward.name)
    assert old_transport == "forward"
    history_before = await _history(client, forward_agent.thread_id, completed_turns=1)
    await client.suspend_sandbox(forward.name)
    await _wait_suspended(client, forward.name)
    await client.resume_sandbox(forward.name)
    resumed = await wait_until_running(client, forward.name)
    assert resumed.uid == forward.uid
    assert resumed.pod is not None
    new_pod_uid, new_credential_id, new_transport = await _identity(forward.name)
    assert new_pod_uid != old_pod_uid
    assert new_credential_id != old_credential_id
    assert new_transport == "forward"
    await _ready_certificate(
        f"runner-{new_credential_id}", resumed.name, str(resumed.uid), new_credential_id, resumed.namespace
    )

    resumed_model = scripted.model(resumed.pod.name, harness)
    turn, history_after, recall = await _turn(
        client,
        forward_agent,
        model=resumed_model,
        command_id=f"resume-{uuid4().hex}",
        prompt="Recall the invented code from our earlier conversation. Reply with only that code.",
        history_turns=2,
        response=marker,
    )
    assert turn.resumed
    assert marker in "\n".join(recall.user_texts)
    assert marker in turn.text
    history_before_serialized = [entry.SerializeToString() for entry in history_before]
    assert [entry.SerializeToString() for entry in history_after[: len(history_before)]] == history_before_serialized
    assert len(history_after) > len(history_before)


if __name__ == "__main__":
    pytest_bazel.main()
