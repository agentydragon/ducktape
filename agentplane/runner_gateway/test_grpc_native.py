"""Production gRPC client → workload auth/discovery → both real harnesses, without the app."""

import asyncio
import shlex
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from more_itertools import one
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.grpc.calls import RefusedError, ServiceError
from agentplane.proto import command_pb2, event_log_pb2, runner_control_pb2, runner_pb2
from agentplane.runner.config import RunnerConfig
from agentplane.runner.runner import Runner, follow_journal
from agentplane.runner.testing import events, launches
from agentplane.runner.testing.scripted_model import ScriptedModel, Text
from agentplane.runner_gateway.client import SandboxControlClient, SandboxRunner
from agentplane.runner_gateway.destinations import DestinationResolver
from agentplane.runner_gateway.runner_proxy import Resources
from agentplane.runner_gateway.testing.auth import CALLER_TOKEN
from agentplane.runner_gateway.testing.connected_runner import runner_connected
from agentplane.runner_gateway.testing.grpc_service import service_client
from agentplane.runner_gateway.testing.resources import build_resources
from agentplane.sandboxes.testing.kubernetes import DESTINATION, OWNER, SandboxCluster
from agentplane.testing.fake_apiserver import TokenVerdict

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

AUDIENCE = "test-native-grpc"


@pytest.fixture
def resources(cluster: SandboxCluster, runner: Runner) -> Resources:
    cluster.fake.tokens[CALLER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{OWNER.namespace}:{OWNER.name}",
        pod_name="test-native-caller",
        pod_uid="test-native-caller-uid",
        audiences=(AUDIENCE,),
    )
    return build_resources(
        principals=cluster.build_principal_resolver(AUDIENCE),
        destinations=DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api)),
        caller_accounts=frozenset({OWNER}),
    )


@pytest.fixture
def bootstrap(workspace: Path) -> str | None:
    """The Sandbox's bootstrap, which the runner runs before its first Session."""
    return f"printf B >> {shlex.quote(str(workspace / 'bootstrap-runs'))}"


@pytest.fixture
def config(config: RunnerConfig, bootstrap: str | None) -> RunnerConfig:
    return replace(config, launch=replace(config.launch, bootstrap=bootstrap))


@pytest.fixture(autouse=True)
async def connected(cluster: SandboxCluster, resources: Resources, runner: Runner) -> AsyncIterator[None]:
    async with runner_connected(cluster, resources, runner):
        yield


@pytest.fixture
async def remote(resources: Resources, token_file: Path) -> AsyncIterator[SandboxControlClient]:
    async with service_client(resources, token_file) as client:
        yield client


async def inspect(runner: SandboxRunner, session_id: str) -> runner_pb2.SessionSummary:
    return one(summary for summary in await runner.list_sessions() if summary.session_id == session_id)


async def submit_input(
    remote: SandboxControlClient, session_id: str, command: command_pb2.Command
) -> event_log_pb2.EventEntry:
    assert command.WhichOneof("operation") == "submit_input"
    response: runner_control_pb2.SubmitInputResponse = await remote.runner_unary(
        DESTINATION,
        lambda stub: stub.SubmitInput,
        runner_control_pb2.SubmitInputRequest(
            session_id=session_id, command_id=command.command_id, text=command.submit_input.text
        ),
        timeout_s=remote.request_timeout_s,
    )
    return response.admission


async def stop_session(remote: SandboxControlClient, session_id: str, command_id: str) -> event_log_pb2.EventEntry:
    response: runner_control_pb2.StopRunnerSessionResponse = await remote.runner_unary(
        DESTINATION,
        lambda stub: stub.StopRunnerSession,
        runner_control_pb2.StopRunnerSessionRequest(session_id=session_id, command_id=command_id),
        timeout_s=remote.request_timeout_s,
    )
    return response.admission


async def complete_turn(
    remote: SandboxControlClient, local: Runner, model: ScriptedModel, session_id: str, command_id: str
) -> list[event_log_pb2.EventEntry]:
    command = command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text="Reply: GRPC_OK"))
    receipt = await submit_input(remote, session_id, command)
    assert receipt.event.command_admitted.command == command
    assert receipt.origin.source_id
    assert receipt.origin.sequence > 0
    # Neither admission nor exact receipt replay waits for model completion.
    assert await submit_input(remote, session_id, command) == receipt
    request = await model.request()
    assert launches.PLATFORM_INSTRUCTIONS in request.system_text
    assert "Test task guidance." in request.system_text
    assert launches.SANDBOX.uid in request.system_text
    await model.reply(request, Text("GRPC_OK"))
    entries: list[event_log_pb2.EventEntry] = []
    async with (
        asyncio.timeout(20),
        aclosing(follow_journal(local.sessions[session_id].journal, receipt.cursor - 1)) as journal,
    ):
        async for entry in journal:
            entries.append(entry)
            if events.turn_completed(entry):
                break
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
    workspace: Path,
    runner: Runner,
) -> None:
    bootstrap_marker = workspace / "bootstrap-runs"
    setup_marker = workspace / "setup-runs"
    spec.instructions = "Test task guidance."
    launch_spec = runner_pb2.SessionSpec()
    launch_spec.CopyFrom(spec)
    setup_script = f"printf S >> {shlex.quote(str(setup_marker))}"
    async with service_client(resources, token_file) as remote:
        destination = remote.runner(DESTINATION)
        requested = uuid4()
        created = await destination.open_session(session_id=requested, spec=launch_spec, setup_script=setup_script)
        session = created.session_id
        async with asyncio.timeout(15):
            while (opened := await inspect(destination, session)).harness_state != runner_pb2.HARNESS_STATE_RUNNING:
                assert opened.setup_state not in (runner_pb2.SETUP_STATE_FAILED, runner_pb2.SETUP_STATE_INTERRUPTED)
                await asyncio.sleep(0.05)
        assert opened.setup_state == runner_pb2.SETUP_STATE_SUCCEEDED
        assert session in opened.spec.instructions
        assert (
            await destination.open_session(session_id=requested, spec=launch_spec, setup_script=setup_script)
        ).spec == opened.spec
        assert (await destination.list_sessions())[0].spec == opened.spec
        # The runner refuses a retry whose spec differs from the one it recorded.
        different_spec = runner_pb2.SessionSpec()
        different_spec.CopyFrom(launch_spec)
        different_spec.instructions = "Different task"
        with pytest.raises(ServiceError) as refused:
            await destination.open_session(session_id=requested, spec=different_spec, setup_script=setup_script)
        assert refused.value.code == grpc.StatusCode.ALREADY_EXISTS
        assert str(refused.value) == f"ALREADY_EXISTS: session {session} exists with a different spec"
        assert bootstrap_marker.read_text() == "B"
        assert setup_marker.read_text() == "S"
        entries = await complete_turn(remote, runner, model, session, "grpc-notice")
        await stop_session(remote, session, "grpc-stop")
        journal = runner.sessions[session].journal
        async with asyncio.timeout(20), aclosing(follow_journal(journal, entries[-1].cursor)) as tail:
            async for entry in tail:
                if events.kind(entry) == "harness_exited":
                    break
        with pytest.raises(RefusedError):
            await submit_input(
                remote,
                session,
                command_pb2.Command(command_id="no-wake", submit_input=command_pb2.SubmitInput(text="no wake")),
            )
        assert (await inspect(destination, session)).harness_state == runner_pb2.HARNESS_STATE_STOPPED
        # Observe the stopped journal too: rejected delivery added no admission and did not wake it.
        assert "no-wake" not in [
            entry.event.command_admitted.command.command_id
            for entry in await journal.since(0, limit=journal.last_cursor)
        ]
    # Stop the service and recover the complete launch spec solely from the runner.
    async with service_client(resources, token_file) as restarted:
        destination = restarted.runner(DESTINATION)
        resumed = await destination.resume(session)
        assert resumed.spec == opened.spec
        assert resumed.harness_state == runner_pb2.HARNESS_STATE_RUNNING
        await complete_turn(restarted, runner, model, session, "grpc-resumed")
        assert (await destination.list_sessions())[0].spec == opened.spec
        invalid_spec = runner_pb2.SessionSpec()
        invalid_spec.CopyFrom(launch_spec)
        invalid_spec.model = ""
        with pytest.raises(ServiceError) as invalid:
            await destination.open_session(session_id=uuid4(), spec=invalid_spec)
        assert invalid.value.code == grpc.StatusCode.INVALID_ARGUMENT
    assert bootstrap_marker.read_text() == "B"
    assert setup_marker.read_text() == "S"


async def test_runner_command_and_resume_do_not_create_unknown_session(remote: SandboxControlClient) -> None:
    runner = remote.runner(DESTINATION)
    unknown = str(uuid4())
    with pytest.raises(ServiceError) as commanded:
        await submit_input(
            remote,
            unknown,
            command_pb2.Command(command_id="unknown-notice", submit_input=command_pb2.SubmitInput(text="no creation")),
        )
    assert commanded.value.code == grpc.StatusCode.NOT_FOUND
    with pytest.raises(ServiceError) as resumed:
        await runner.resume(unknown)
    assert resumed.value.code == grpc.StatusCode.NOT_FOUND
    assert not await runner.list_sessions()


async def test_failed_setup_cannot_be_resumed(remote: SandboxControlClient, spec: runner_pb2.SessionSpec) -> None:
    runner = remote.runner(DESTINATION)
    created = await runner.open_session(session_id=uuid4(), spec=spec, setup_script="exit 42")
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(15), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            opened = await inspect(runner, created.session_id)
            assert opened.setup_state != runner_pb2.SETUP_STATE_RUNNING
    assert opened.setup_state == runner_pb2.SETUP_STATE_FAILED
    with pytest.raises(RefusedError):
        await runner.resume(created.session_id)


async def test_invalid_launch_never_creates(remote: SandboxControlClient) -> None:
    runner = remote.runner(DESTINATION)
    invalid_specs = [runner_pb2.SessionSpec(), runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CODEX, model="m")]
    for invalid in invalid_specs:
        with pytest.raises(ServiceError) as rejected:
            await runner.open_session(session_id=uuid4(), spec=invalid)
        assert rejected.value.code == grpc.StatusCode.INVALID_ARGUMENT
    assert not await runner.list_sessions()


@pytest.mark.parametrize("bootstrap", ["exit 42"])
async def test_failed_bootstrap_never_creates(remote: SandboxControlClient, spec: runner_pb2.SessionSpec) -> None:
    runner = remote.runner(DESTINATION)
    with pytest.raises(RefusedError):
        await runner.open_session(session_id=uuid4(), spec=spec)
    assert not await runner.list_sessions()


if __name__ == "__main__":
    pytest_bazel.main()
