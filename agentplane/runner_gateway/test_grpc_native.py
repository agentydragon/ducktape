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
from google.protobuf.json_format import MessageToDict
from kubernetes_asyncio import client as k8s_client
from more_itertools import one
from sqlalchemy.ext.asyncio import AsyncEngine
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.grpc_calls import ServiceError
from agentplane.proto import command_pb2, event_log_pb2, runner_pb2
from agentplane.proto.sandbox_service_pb2 import SandboxBinding, SessionDefaults
from agentplane.runner import service
from agentplane.runner.errors import RunnerError
from agentplane.runner.testing import events
from agentplane.runner.testing.scripted_model import ScriptedModel, Text
from agentplane.runner_gateway.client import Runner, RunnerGatewayClient
from agentplane.runner_gateway.destinations import DestinationResolver
from agentplane.runner_gateway.grpc_api import Resources
from agentplane.runner_gateway.testing.grpc_service import service_client
from agentplane.runner_gateway.testing.resources import build_resources
from agentplane.sandbox_service.testing.grpc_service import CALLER_TOKEN
from agentplane.sandbox_service.testing.kubernetes import DESTINATION, OWNER, SANDBOX_UID, Cluster, runner_connected
from agentplane.testing.fake_apiserver import TokenVerdict

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

AUDIENCE = "test-native-grpc"


@pytest.fixture
def resources(cluster: Cluster, runner: service.Runner, engine: AsyncEngine) -> Resources:
    cluster.fake.tokens[CALLER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{OWNER.namespace}:{OWNER.name}",
        pod_name="test-native-caller",
        pod_uid="test-native-caller-uid",
        audiences=(AUDIENCE,),
    )
    return build_resources(
        engine,
        principals=cluster.build_principal_resolver(AUDIENCE),
        destinations=DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api)),
        caller_accounts=frozenset({OWNER}),
        platform_instructions="Test backend-owned guidance.",
        # A real runner answers here, so admission needs room for delivery's two-second recheck:
        # under one second, a single missed dispatch wakeup or control-stream reconnect fails it.
        runner_admission_ack_timeout_s=15,
    )


@pytest.fixture(autouse=True)
async def connected(cluster: Cluster, resources: Resources, runner: service.Runner) -> AsyncIterator[None]:
    async with runner_connected(cluster, resources, runner):
        yield


@pytest.fixture
async def remote(resources: Resources, token_file: Path) -> AsyncIterator[RunnerGatewayClient]:
    async with service_client(resources, token_file) as client:
        yield client


async def inspect(runner: Runner, session_id: str) -> runner_pb2.SessionSummary:
    return one(summary for summary in await runner.list_sessions() if summary.session_id == session_id)


async def complete_turn(
    remote: RunnerGatewayClient, local: service.Runner, model: ScriptedModel, session_id: str, command_id: str
) -> list[event_log_pb2.EventEntry]:
    command = command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text="Reply: GRPC_OK"))
    receipt = await remote.command(DESTINATION, session_id, command)
    assert receipt.event.command_admitted.command == command
    assert receipt.origin.source_id
    assert receipt.origin.sequence > 0
    # Neither admission nor exact receipt replay waits for model completion.
    assert await remote.command(DESTINATION, session_id, command) == receipt
    request = await model.request()
    assert "Test backend-owned guidance." in request.system_text
    assert "Test task guidance." in request.system_text
    assert "New platform guidance." not in request.system_text
    assert str(SANDBOX_UID) in request.system_text
    await model.reply(request, Text("GRPC_OK"))
    entries: list[event_log_pb2.EventEntry] = []
    async with (
        asyncio.timeout(20),
        aclosing(service.follow_journal(local.sessions[session_id].journal, receipt.cursor - 1)) as journal,
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
    cluster: Cluster,
    workspace: Path,
    runner: service.Runner,
) -> None:
    bootstrap_marker = workspace / "bootstrap-runs"
    setup_marker = workspace / "setup-runs"
    cluster.set_binding(
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
        )
    )
    async with service_client(resources, token_file) as remote:
        destination = remote.runner(DESTINATION)
        requested = uuid4()
        created = await destination.create(session_id=requested, spec={})
        session = created.session_id
        async with asyncio.timeout(15):
            while (opened := await inspect(destination, session)).harness_state != runner_pb2.HARNESS_STATE_RUNNING:
                assert opened.setup_state not in (runner_pb2.SETUP_STATE_FAILED, runner_pb2.SETUP_STATE_INTERRUPTED)
                await asyncio.sleep(0.05)
        assert opened.setup_state == runner_pb2.SETUP_STATE_SUCCEEDED
        assert session in opened.spec.instructions
        assert (await destination.create(session_id=requested, spec={})).attached.spec == opened.spec
        assert (await destination.list_sessions())[0].spec == opened.spec
        # The runner refuses a retry whose spec differs from the one it recorded.
        with pytest.raises(RunnerError):
            await destination.create(session_id=requested, spec={"instructions": "Different task"})
        assert bootstrap_marker.read_text() == "B"
        assert setup_marker.read_text() == "S"
        entries = await complete_turn(remote, runner, model, session, "grpc-notice")
        stop = command_pb2.Command(command_id="grpc-stop", stop_runner_session=command_pb2.StopRunnerSession())
        await remote.command(DESTINATION, session, stop)
        journal = runner.sessions[session].journal
        async with asyncio.timeout(20), aclosing(service.follow_journal(journal, entries[-1].cursor)) as tail:
            async for entry in tail:
                if events.kind(entry) == "harness_exited":
                    break
        with pytest.raises(RunnerError):
            await remote.command(
                DESTINATION,
                session,
                command_pb2.Command(command_id="no-wake", submit_input=command_pb2.SubmitInput(text="no wake")),
            )
        assert (await inspect(destination, session)).harness_state == runner_pb2.HARNESS_STATE_STOPPED
        # Observe the stopped journal too: rejected delivery added no admission and did not wake it.
        assert "no-wake" not in [
            entry.event.command_admitted.command.command_id
            for entry in await journal.since(0, limit=journal.last_cursor)
        ]
    # Stop the service, change its configuration and stored defaults, and recover solely from the runner.
    cluster.set_binding(SandboxBinding(bootstrap="exit 42", session_defaults=SessionDefaults(model="changed")))
    async with service_client(
        replace(resources, platform_instructions="New platform guidance."), token_file
    ) as restarted:
        destination = restarted.runner(DESTINATION)
        resumed = await restarted.resume(DESTINATION, session)
        assert resumed.spec == opened.spec
        assert resumed.harness_state == runner_pb2.HARNESS_STATE_RUNNING
        await complete_turn(restarted, runner, model, session, "grpc-resumed")
        assert (await destination.list_sessions())[0].spec == opened.spec
        with pytest.raises(RunnerError):
            await destination.create(
                session_id=uuid4(), spec={"harness": "HARNESS_CODEX", "cwd": "/workspace", "model": "test"}
            )
    assert bootstrap_marker.read_text() == "B"
    assert setup_marker.read_text() == "S"


async def test_observe_command_and_resume_do_not_create_unknown_session(remote: RunnerGatewayClient) -> None:
    runner = remote.runner(DESTINATION)
    unknown = str(uuid4())
    with pytest.raises(ServiceError) as commanded:
        await remote.command(
            DESTINATION,
            unknown,
            command_pb2.Command(command_id="unknown-notice", submit_input=command_pb2.SubmitInput(text="no creation")),
        )
    assert commanded.value.code == grpc.StatusCode.NOT_FOUND
    with pytest.raises(ServiceError) as resumed:
        await remote.resume(DESTINATION, unknown)
    assert resumed.value.code == grpc.StatusCode.NOT_FOUND
    assert not await runner.list_sessions()


async def test_failed_setup_cannot_be_resumed(remote: RunnerGatewayClient, spec: runner_pb2.SessionSpec) -> None:
    runner = remote.runner(DESTINATION)
    created = await runner.create(session_id=uuid4(), spec=MessageToDict(spec), setup_script="exit 42")
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(15), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            opened = await inspect(runner, created.session_id)
            assert opened.setup_state != runner_pb2.SETUP_STATE_RUNNING
    assert opened.setup_state == runner_pb2.SETUP_STATE_FAILED
    with pytest.raises(RunnerError):
        await remote.resume(DESTINATION, created.session_id)


async def test_invalid_launch_and_failed_bootstrap_never_create(
    remote: RunnerGatewayClient, cluster: Cluster, spec: runner_pb2.SessionSpec
) -> None:
    runner = remote.runner(DESTINATION)
    invalid_specs: list[dict[str, object]] = [{}, {"harness": "HARNESS_CODEX", "model": "m"}]
    for invalid in invalid_specs:
        with pytest.raises(ServiceError) as rejected:
            await runner.create(session_id=uuid4(), spec=invalid)
        assert rejected.value.code == grpc.StatusCode.INVALID_ARGUMENT
    cluster.set_binding(SandboxBinding(bootstrap="exit 42"))
    with pytest.raises(RunnerError):
        await runner.create(session_id=uuid4(), spec=MessageToDict(spec))
    assert not await runner.list_sessions()


if __name__ == "__main__":
    pytest_bazel.main()
