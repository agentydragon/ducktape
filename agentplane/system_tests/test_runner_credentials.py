"""Runner mTLS certificates authenticate Pod and VM sessions across incarnation changes."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from tenacity import AsyncRetrying, retry_if_exception_type, retry_if_result, stop_after_delay, wait_fixed

from agentplane.app.testing.client import REQUEST_SECONDS, Client
from agentplane.app.testing.models import ThreadView
from agentplane.proto import command_pb2, event_log_pb2, event_pb2, runner_pb2
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2
from agentplane.sandbox_resource import OperatingMode
from agentplane.system_tests.agent import TURN_SECONDS, WORKING_DIRECTORY, Agent, Turn
from agentplane.system_tests.cluster import kubectl
from agentplane.system_tests.conftest import (
    POD_TEMPLATE,
    VM_TEMPLATE,
    VM_WORKING_DIRECTORY,
    Sandboxes,
    models_offered_for,
    traced_vm_scenario,
    wait_until_running,
)
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from agentplane.testing.scripted_model import ModelRequest, ScriptedModel, ShellCall, Text

_RUNNER_CREDENTIAL_LABEL = "agentplane.allegedly.works/runner-credential"
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
    async with asyncio.TaskGroup() as turns:
        collecting = turns.create_task(agent.next_turn(awaiting_command_id=command_id))
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
    turn = collecting.result()
    assert turn.status == event_pb2.TURN_STATUS_COMPLETED
    entries = await _history(client, agent.thread_id, completed_turns=history_turns)
    assert any(
        entry.event.HasField("command_admitted") and entry.event.command_admitted.command.command_id == command_id
        for entry in entries
    ), f"History did not retain command admission {command_id}"
    # FollowSession exposes committed archive entries before History's asynchronous fold applies
    # them. The Thread summary is authoritative only once it covers this completed turn.
    view: ThreadView = await AsyncRetrying(
        stop=stop_after_delay(REQUEST_SECONDS),
        wait=wait_fixed(0.05),
        retry=retry_if_result(lambda row: row.last_cursor < entries[-1].cursor),
    )(client.thread, agent.thread_id)
    assert view.last_turn_status == "TURN_STATUS_COMPLETED"
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


async def _identity(pod_name: str) -> tuple[str, str]:
    pod = json.loads(await kubectl("get", "pod", pod_name, "-o", "json"))
    metadata = pod["metadata"]
    labels = metadata["labels"]
    return (metadata["uid"], labels[_RUNNER_CREDENTIAL_LABEL])


async def _ready_certificate(
    name: str, sandbox_name: str, sandbox_uid: str, credential_id: str, namespace: str
) -> None:
    certificate = json.loads(await kubectl("get", "certificate", name, "-o", "json"))
    spec = certificate["spec"]
    assert spec["secretName"] == f"runner-{credential_id}"
    assert spec["uris"] == [f"spiffe://agentplane/runner/{namespace}/{sandbox_uid}/{credential_id}"]
    assert set(spec["usages"]) == {"digital signature", "server auth"}
    assert any(
        condition["type"] == "Ready" and condition["status"] == "True"
        for condition in certificate["status"]["conditions"]
    )
    secret = json.loads(await kubectl("get", "secret", f"runner-{credential_id}", "-o", "json"))
    assert secret["metadata"]["annotations"]["cert-manager.io/certificate-name"] == name
    assert secret["metadata"]["labels"][_SANDBOX_LABEL] == sandbox_name
    assert secret["metadata"]["labels"][_RUNNER_CREDENTIAL_LABEL] == credential_id
    requests = json.loads(await kubectl("get", "certificaterequests", "-o", "json"))["items"]
    issued = [
        request
        for request in requests
        if any(owner["uid"] == certificate["metadata"]["uid"] for owner in request["metadata"]["ownerReferences"])
        and any(c["type"] == "Ready" and c["status"] == "True" for c in request.get("status", {}).get("conditions", []))
    ]
    assert len(issued) == 1
    assert any(c["type"] == "Approved" and c["status"] == "True" for c in issued[0]["status"]["conditions"])
    assert base64.b64decode(issued[0]["status"]["certificate"]) == base64.b64decode(secret["data"]["tls.crt"])


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


async def _open_agent(
    client: Client, name: str, harness: runner_pb2.Harness, model_name: str, *, cwd: str = WORKING_DIRECTORY
) -> Agent:
    agent = await Agent.open(client, sandbox=name, harness=harness, model=model_name, cwd=cwd)
    await client.wait_for_thread_feed(agent.thread_id, timeout_s=REQUEST_SECONDS)
    return agent


async def test_cert_manager_reissues_a_deleted_tls_secret() -> None:
    name = f"test-reissue-{uuid4().hex[:8]}"
    await kubectl(
        "apply",
        "-f",
        "-",
        body={
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "metadata": {"name": name},
            "spec": {
                "secretName": name,
                "issuerRef": {"name": "agentplane-runner-ca", "kind": "Issuer"},
                "uris": [f"spiffe://agentplane/test-reissue/{name}"],
                "privateKey": {"algorithm": "ECDSA", "size": 256, "rotationPolicy": "Always"},
                "usages": ["digital signature", "server auth"],
            },
        },
    )
    await kubectl("wait", "--for=condition=Ready", f"certificate/{name}", f"--timeout={REQUEST_SECONDS}s")
    original = json.loads(await kubectl("get", "secret", name, "-o", "json"))
    certificate = json.loads(await kubectl("get", "certificate", name, "-o", "json"))
    revision = certificate["status"]["revision"]
    await kubectl("delete", "secret", name, "--wait=false")
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(REQUEST_SECONDS),
        wait=wait_fixed(1),
        retry=retry_if_exception_type(AssertionError),
        reraise=True,
    ):
        with attempt:
            encoded = await kubectl("get", "secret", name, "--ignore-not-found", "-o", "json")
            assert encoded
            replacement = json.loads(encoded)
            assert replacement["metadata"]["uid"] != original["metadata"]["uid"]
            certificate = json.loads(await kubectl("get", "certificate", name, "-o", "json"))
            assert certificate["status"]["revision"] > revision
            assert any(c["type"] == "Ready" and c["status"] == "True" for c in certificate["status"]["conditions"])
    assert replacement["data"]["tls.crt"] != original["data"]["tls.crt"]


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_pod_mtls_session_survives_replacement_and_keeps_history(
    client: Client, sandbox: Sandboxes, harness: runner_pb2.Harness, offered_model: str, scripted: ScriptedUpstream
) -> None:
    view = await sandbox(f"systest-runner-pod-{uuid4().hex[:8]}", template=POD_TEMPLATE)
    assert view.pod is not None
    old_pod_uid, old_credential_id = await _identity(view.pod.name)
    await _ready_certificate(f"runner-{old_credential_id}", view.name, str(view.uid), old_credential_id, view.namespace)

    agent = await _open_agent(client, view.name, harness, offered_model)
    model = scripted.model(view.pod.name, harness)
    marker = f"POD_CONTEXT_{uuid4().hex}"
    turn, history_before, _ = await _turn(
        client,
        agent,
        model=model,
        command_id=f"pod-{uuid4().hex}",
        prompt=f"Remember this invented non-secret code for our next turn: {marker}. Run a shell check.",
        shell_command="printf 'POD_MTLS_OK\\n'",
        response="POD_MTLS_OK",
    )
    assert "POD_MTLS_OK" in "\n".join(turn.tool_outputs)

    await client.suspend_sandbox(view.name)
    await _wait_suspended(client, view.name)
    await client.resume_sandbox(view.name)
    resumed = await wait_until_running(client, view.name)
    assert resumed.uid == view.uid
    assert resumed.pod is not None
    new_pod_uid, new_credential_id = await _identity(resumed.pod.name)
    assert new_pod_uid != old_pod_uid
    assert new_credential_id != old_credential_id
    await _ready_certificate(
        f"runner-{new_credential_id}", resumed.name, str(resumed.uid), new_credential_id, resumed.namespace
    )

    resumed_model = scripted.model(resumed.pod.name, harness)
    turn, history_after, recall = await _turn(
        client,
        agent,
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


async def _run_vm_mtls_certificate_authenticates_a_session(
    client: Client, sandbox: Sandboxes, harness: runner_pb2.Harness, offered_model: str, scripted: ScriptedUpstream
) -> None:
    view = await sandbox(f"systest-runner-vm-{uuid4().hex[:8]}", template=VM_TEMPLATE)
    assert view.pod is not None
    pod_uid, credential_id = await _identity(view.pod.name)
    await _ready_certificate(f"runner-{credential_id}", view.name, str(view.uid), credential_id, view.namespace)

    agent = await _open_agent(client, view.name, harness, offered_model, cwd=VM_WORKING_DIRECTORY)
    model = scripted.model(view.pod.name, harness)
    turn, _, _ = await _turn(
        client,
        agent,
        model=model,
        command_id=f"vm-{uuid4().hex}",
        prompt="Run a shell check in the VM runner.",
        shell_command="printf 'VM_MTLS_OK\\n'",
        response="VM_MTLS_OK",
    )
    assert "VM_MTLS_OK" in "\n".join(turn.tool_outputs)
    current_pod_uid, current_credential_id = await _identity(view.pod.name)
    assert (current_pod_uid, current_credential_id) == (pod_uid, credential_id)


@pytest.mark.vm_sandboxes
@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_vm_mtls_certificates_authenticate_both_harnesses(
    client: Client, sandbox: Sandboxes, scripted: ScriptedUpstream
) -> None:
    # pytest-asyncio serializes parametrized cases; explicit tasks overlap the independent boots
    # while sharing this stack and its Pod-routed scripted server.
    harnesses = [runner_pb2.HARNESS_CLAUDE, runner_pb2.HARNESS_CODEX]
    offered_models = await models_offered_for(client, harnesses)
    async with asyncio.TaskGroup() as scenarios:
        for harness in harnesses:
            name = f"runner-vm-mtls-{runner_pb2.Harness.Name(harness).lower()}"
            scenarios.create_task(
                traced_vm_scenario(
                    name,
                    _run_vm_mtls_certificate_authenticates_a_session(
                        client, sandbox, harness, offered_models[harness], scripted
                    ),
                )
            )


if __name__ == "__main__":
    pytest_bazel.main()
