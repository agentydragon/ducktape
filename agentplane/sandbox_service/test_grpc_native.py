"""Production gRPC client → workload auth/discovery → both real harnesses, without the app."""

import asyncio
import shlex
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import grpc
import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict
from kubernetes_asyncio import client as k8s_client

from agentplane.protocol import command_pb2, event_log_pb2
from agentplane.runner import protocol_pb2 as runner_pb2
from agentplane.runner.errors import RunnerError, StreamClosedError
from agentplane.runner.testing import events
from agentplane.runner.testing.fixtures import RunnerHandle
from agentplane.runner.testing.scripted_model import ScriptedModel, Text
from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.binding_storage import write_binding
from agentplane.sandbox_service.client import Runner, SandboxServiceClient, ServiceError
from agentplane.sandbox_service.destinations import DestinationResolver
from agentplane.sandbox_service.grpc_api import Resources
from agentplane.sandbox_service.kubernetes_views import SANDBOX_BINDING_ANNOTATION
from agentplane.sandbox_service.protocol_pb2 import SandboxBinding, SandboxDestination, SessionDefaults
from agentplane.sandbox_service.testing.grpc_service import service_client
from agentplane.sandbox_service.testing.kubernetes import ACCOUNT, SANDBOX, SANDBOX_UID, Cluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.agent_sandbox import SANDBOXES_PLURAL

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

TOKEN = "test-native-grpc-token"
AUDIENCE = "test-native-grpc"
OWNER = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name=ACCOUNT)
DESTINATION = SandboxDestination(
    owner=protocol_pb2.ServiceAccount(namespace=OWNER.namespace, name=OWNER.name),
    sandbox=SANDBOX,
    sandbox_uid=SANDBOX_UID,
)
SESSION = "grpc-session"


@pytest.fixture
def resources(cluster: Cluster, runner: RunnerHandle) -> Resources:
    cluster.fake.tokens[TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{OWNER.namespace}:{OWNER.name}",
        pod_name="test-native-caller",
        pod_uid="test-native-caller-uid",
        audiences=(AUDIENCE,),
    )
    return Resources(
        principals=WorkloadPrincipalResolver(
            authentication=k8s_client.AuthenticationV1Api(cluster.api),
            audience=AUDIENCE,
            allowed_service_account_namespaces={SANDBOX_NAMESPACE},
        ),
        provisioning=cluster.provisioning,
        destinations=DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api), runner.port),
        caller_accounts=frozenset({OWNER}),
        platform_instructions="Test backend-owned guidance.",
    )


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "service-token"
    path.write_text(TOKEN)
    return path


@pytest.fixture
async def remote(resources: Resources, token_file: Path) -> AsyncIterator[SandboxServiceClient]:
    async with service_client(resources, token_file) as client:
        yield client


def set_binding(cluster: Cluster, binding: SandboxBinding) -> None:
    cluster.fake.objects[SANDBOXES_PLURAL][SANDBOX]["metadata"].setdefault("annotations", {})[
        SANDBOX_BINDING_ANNOTATION
    ] = write_binding(binding)


async def inspect(remote: SandboxServiceClient) -> runner_pb2.Attached:
    attachment = await remote.runner(DESTINATION).attach(SESSION)
    try:
        return attachment.attached
    finally:
        attachment.cancel()


async def complete_turn(runner: Runner, model: ScriptedModel, command_id: str) -> list[event_log_pb2.EventEntry]:
    command = command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text="Reply: GRPC_OK"))
    receipt = await runner.command(SESSION, command, after_cursor=0)
    assert receipt.event.command_admitted.command == command
    assert receipt.origin.source_id
    assert receipt.origin.sequence > 0
    # Neither admission nor exact receipt replay waits for model completion.
    assert await runner.command(SESSION, command, after_cursor=0) == receipt
    request = await model.request()
    assert "Test backend-owned guidance." in request.system_text
    assert "Test task guidance." in request.system_text
    assert "New platform guidance." not in request.system_text
    assert str(SANDBOX_UID) in request.system_text
    await model.reply(request, Text("GRPC_OK"))
    attachment = await runner.attach(SESSION, after_cursor=receipt.cursor - 1)
    entries: list[event_log_pb2.EventEntry] = []
    try:
        async with asyncio.timeout(20):
            while True:
                entry = await attachment.next_entry()
                entries.append(entry)
                if events.turn_completed(entry):
                    break
    finally:
        attachment.cancel()
    assert events.of_kind(entries, "command_admitted") == [receipt]
    assert any(
        command.command_id in e.event.harness_user_message_confirmed.origin_command_ids
        for e in events.of_kind(entries, "harness_user_message_confirmed")
    )
    return entries


async def test_launch_delivery_and_restart_preserve_evidence_and_configuration(
    resources: Resources,
    token_file: Path,
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    cluster: Cluster,
    workspace: Path,
) -> None:
    bootstrap_marker = workspace / "bootstrap-runs"
    setup_marker = workspace / "setup-runs"
    set_binding(
        cluster,
        SandboxBinding(
            bootstrap=f"printf B >> {shlex.quote(str(bootstrap_marker))}",
            session_defaults=SessionDefaults(
                harness=spec.harness,
                model=spec.model,
                reasoning_effort=spec.reasoning_effort,
                cwd=spec.cwd,
                instructions="Test task guidance.",
                setup_script=f"printf S >> {shlex.quote(str(setup_marker))}",
            ),
        ),
    )
    async with service_client(resources, token_file) as remote:
        runner = remote.runner(DESTINATION)
        opened = await runner.open(SESSION, {})
        async with asyncio.timeout(15):
            while opened.harness_state != runner_pb2.HARNESS_STATE_RUNNING:
                assert opened.setup_state not in (runner_pb2.SETUP_STATE_FAILED, runner_pb2.SETUP_STATE_INTERRUPTED)
                await asyncio.sleep(0.05)
                opened = await inspect(remote)
        assert opened.setup_state == runner_pb2.SETUP_STATE_SUCCEEDED
        assert SESSION in opened.spec.instructions
        assert (await runner.open(SESSION, {})).spec == opened.spec
        assert (await runner.list_sessions())[0].spec == opened.spec
        with pytest.raises(RunnerError):
            await runner.open(SESSION, {"instructions": "Different task"})
        assert bootstrap_marker.read_text() == "B"
        assert setup_marker.read_text() == "S"
        entries = await complete_turn(runner, model, "grpc-notice")
        stop = command_pb2.Command(command_id="grpc-stop", stop_runner_session=command_pb2.StopRunnerSession())
        await runner.command(SESSION, stop, after_cursor=entries[-1].cursor)
        tail = await runner.attach(SESSION, after_cursor=entries[-1].cursor)
        try:
            async with asyncio.timeout(20):
                while True:
                    try:
                        entry = await tail.next_entry()
                    except StreamClosedError:
                        break
                    assert entry.cursor > entries[-1].cursor
        finally:
            tail.cancel()
        with pytest.raises(RunnerError):
            await runner.command(
                SESSION,
                command_pb2.Command(command_id="no-wake", submit_input=command_pb2.SubmitInput(text="no wake")),
                after_cursor=0,
            )
        assert (await inspect(remote)).harness_state == runner_pb2.HARNESS_STATE_STOPPED
        # Observe the stopped journal too: rejected delivery added no admission and did not wake it.
        stopped = await runner.attach(SESSION)
        try:
            async with asyncio.timeout(20):
                cursor = 0
                # A fresh follow of a stopped session can remain open for a future resume.
                # Check its retained snapshot prefix, not an EOF that this attachment needn't see.
                while cursor < stopped.attached.last_cursor:
                    entry = await stopped.next_entry()
                    assert entry.cursor > cursor
                    cursor = entry.cursor
                    assert entry.event.command_admitted.command.command_id != "no-wake"
        finally:
            stopped.cancel()
    # Stop the service, change its configuration and stored defaults, and recover solely from the runner.
    set_binding(cluster, SandboxBinding(bootstrap="exit 42", session_defaults=SessionDefaults(model="changed")))
    async with service_client(
        replace(resources, platform_instructions="New platform guidance."), token_file
    ) as restarted:
        runner = restarted.runner(DESTINATION)
        resumed = await runner.resume(SESSION)
        assert resumed.spec == opened.spec
        assert resumed.harness_state == runner_pb2.HARNESS_STATE_RUNNING
        await complete_turn(runner, model, "grpc-resumed")
        assert (await runner.list_sessions())[0].spec == opened.spec
        with pytest.raises(RunnerError):
            await runner.open("new-session", {"harness": "HARNESS_CODEX", "cwd": "/workspace", "model": "test"})
    assert bootstrap_marker.read_text() == "B"
    assert setup_marker.read_text() == "S"


async def test_observe_command_and_resume_do_not_create_unknown_session(remote: SandboxServiceClient) -> None:
    runner = remote.runner(DESTINATION)
    with pytest.raises(RunnerError):
        await inspect(remote)
    with pytest.raises(RunnerError):
        await runner.attach(SESSION)
    with pytest.raises(RunnerError):
        await runner.command(
            SESSION,
            command_pb2.Command(command_id="unknown-notice", submit_input=command_pb2.SubmitInput(text="no creation")),
            after_cursor=0,
        )
    with pytest.raises(RunnerError):
        await runner.resume(SESSION)
    assert not await runner.list_sessions()


async def test_failed_setup_cannot_be_resumed(remote: SandboxServiceClient, spec: runner_pb2.SessionSpec) -> None:
    runner = remote.runner(DESTINATION)
    opened = await runner.open(SESSION, MessageToDict(spec), setup_script="exit 42")
    async with asyncio.timeout(15):
        while opened.setup_state == runner_pb2.SETUP_STATE_RUNNING:
            await asyncio.sleep(0.05)
            opened = await inspect(remote)
    assert opened.setup_state == runner_pb2.SETUP_STATE_FAILED
    with pytest.raises(RunnerError):
        await runner.resume(SESSION)


async def test_invalid_launch_and_failed_bootstrap_never_create(
    remote: SandboxServiceClient, cluster: Cluster, spec: runner_pb2.SessionSpec
) -> None:
    runner = remote.runner(DESTINATION)
    invalid_specs: list[dict[str, object]] = [{}, {"harness": "HARNESS_CODEX", "model": "m"}]
    for invalid in invalid_specs:
        with pytest.raises(ServiceError) as rejected:
            await runner.open(SESSION, invalid)
        assert rejected.value.code == grpc.StatusCode.INVALID_ARGUMENT
    set_binding(cluster, SandboxBinding(bootstrap="exit 42"))
    with pytest.raises(RunnerError):
        await runner.open(SESSION, MessageToDict(spec))
    assert not await runner.list_sessions()


if __name__ == "__main__":
    pytest_bazel.main()
