"""A routed Runtime command cannot cross the real History seal acknowledgement."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from python.runfiles import Runfiles
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.env.config import Endpoint, EnvironmentRuntimeControl
from agentplane.env.e2e.testing import events, launches
from agentplane.env.e2e.testing.config import RunnerTestConfig
from agentplane.env.e2e.testing.gateway import bind_incarnation
from agentplane.env.e2e.testing.model_config import model_config
from agentplane.env.e2e.testing.process import history_snapshot
from agentplane.env.e2e.testing.process_fixtures import RunnerProcess, pid1_container
from agentplane.grpc.calls import RefusedError, ServiceError
from agentplane.harness_host import harness_host_pb2
from agentplane.history_service.testing.go_service import GoHistoryService
from agentplane.proto import event_log_pb2, event_pb2, session_pb2
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.session_dispatcher.client import SandboxControlClient
from agentplane.session_dispatcher.dispatcher.api.session import session_dispatcher_pb2
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient, accepted_session, admitted
from agentplane.session_dispatcher.testing.gateway_process import GatewayProcess
from agentplane.testing.scripted_model import ScriptedModel, Text
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf

_TEST_RUNTIME_BINARY = (
    "ducktape/agentplane/env/runtime/cmd/deletion-test-environment-runtime_/deletion-test-environment-runtime"
)
_GATE_DIRECTORY = "deletion-test-admission"


@pytest.fixture
def controller_enabled() -> bool:
    """Opt this case into the distinct production Controller Quiesce authorization."""
    return True


@pytest.fixture
def runtime_binary() -> Path:
    """Use the system-test gate, which calls the production Runtime handler after release."""
    return Runfiles.CreateOrRaise().root() / _TEST_RUNTIME_BINARY


@pytest.fixture
async def start_runtime(
    config: RunnerTestConfig,
    control: EnvironmentRuntimeControl,
    ingress: Endpoint,
    gateway_service: GatewayProcess,
    sandbox: SandboxCluster,
    runtime_binary: Path,
    tmp_path: Path,
) -> AsyncIterator[Callable[[str], Awaitable[tuple[RunnerProcess, str]]]]:
    """Start the same real Runtime binary on a caller-supplied Pod incarnation UID."""
    environment = {**config.harness_environment, "ANTHROPIC_AUTH_TOKEN": launches.TOKEN, "OTEL_SDK_DISABLED": "true"}
    remote = gateway_service.environment_runtime_client(request_timeout_s=2)
    controller_credentials = gateway_service.environment_service.channel_credentials(
        "spiffe://agentplane/sandbox-controller"
    )
    async with model_config() as model_endpoint, AsyncExitStack() as processes:
        command = launches.command(
            config, control=control, ingress=ingress, model_config_target=model_endpoint, runner_binary=runtime_binary
        )

        async def stop(process: RunnerProcess) -> None:
            try:
                await process.stop()
            finally:
                await process.close()

        async def start(incarnation_uid: str) -> tuple[RunnerProcess, str]:
            container = await asyncio.to_thread(pid1_container, command, tmp_path, environment)
            await asyncio.to_thread(container.__enter__)
            process = RunnerProcess(container)
            processes.push_async_callback(stop, process)
            await bind_incarnation(control, sandbox.uid, incarnation_uid, controller_credentials)
            boot_id: str | None = None
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(15),
                wait=wait_fixed(0.05),
                retry=retry_if_exception_type(AssertionError),
                reraise=True,
            ):
                with attempt:
                    try:
                        status = await remote.runner_unary(
                            sandbox.destination,
                            lambda stub: stub.GetStatus,
                            session_pb2.GetStatusRequest(),
                            expected_incarnation_uid=incarnation_uid,
                        )
                    except ServiceError as error:
                        if error.code.name not in {"UNAVAILABLE", "ABORTED"}:
                            raise
                        raise AssertionError(
                            f"Gateway is not yet serving Runtime incarnation {incarnation_uid}: {error}"
                        ) from error
                    assert status.environment_runtime_boot_id
                    boot_id = status.environment_runtime_boot_id
            assert boot_id is not None
            return process, boot_id

        yield start


async def _recorded_turn(
    history: GoHistoryService,
    session_id: str,
    after_cursor: int,
    predicate: Callable[[event_log_pb2.EventEntry], bool] = events.turn_completed,
) -> list[event_log_pb2.EventEntry]:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(30), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            entries = list(
                (await history.reader.read_session_events(session_id, after_cursor=after_cursor, limit=1000)).entries
            )
            assert any(predicate(entry) for entry in entries)
            return entries
    raise AssertionError("History did not commit the expected Runtime event")


async def _wait_for_file[T](path: Path, expected: str, *, task: asyncio.Task[T] | None = None) -> None:
    deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < deadline:
        if await asyncio.to_thread(path.exists):
            actual = await asyncio.to_thread(path.read_text)
            if actual == expected:
                return
            raise AssertionError(f"{path.name} = {actual!r}, wanted {expected!r}")
        if task is not None and task.done():
            await task
            raise AssertionError(f"{path.name} was not created before the RPC completed")
        await asyncio.sleep(0.05)
    raise TimeoutError(f"Runtime did not create {path} within 20 seconds")


def _archive_gate(gate: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("command", "entered", "release", "result"):
        source = gate / name
        if source.exists():
            shutil.copyfile(source, destination / name)


async def _history_page(history: GoHistoryService, session_id: str):
    return await history.reader.read_session_events(session_id, after_cursor=0, limit=1000)


async def _wait_for_quiesced(
    runtime_client: SandboxControlClient, sandbox: SandboxCluster, incarnation_uid: str, boot_id: str
) -> None:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(30), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            status = await runtime_client.runner_unary(
                sandbox.destination,
                lambda stub: stub.GetStatus,
                session_pb2.GetStatusRequest(),
                expected_incarnation_uid=incarnation_uid,
            )
            assert status.environment_runtime_boot_id == boot_id, status
            assert status.lifecycle.stage == session_pb2.ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_QUIESCED, status.lifecycle


async def _wait_for_session_status(
    runtime_client: SandboxControlClient,
    sandbox: SandboxCluster,
    session_id: str,
    incarnation_uid: str,
    boot_id: str,
    *,
    expected_harness_state: int | None = None,
) -> harness_host_pb2.Session:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(30), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            status = await runtime_client.runner_unary(
                sandbox.destination,
                lambda stub: stub.GetStatus,
                session_pb2.GetStatusRequest(),
                expected_incarnation_uid=incarnation_uid,
            )
            assert status.environment_runtime_boot_id == boot_id, status
            sessions = await runtime_client.list_sessions(sandbox.destination, expected_incarnation_uid=incarnation_uid)
            session = next((row for row in sessions if row.session_id == session_id), None)
            assert session is not None, f"Session {session_id} not visible in HarnessHost inventory: {sessions}"
            assert session.WhichOneof("host") == "environment_hosted", session
            if expected_harness_state is not None:
                assert session.environment_hosted.harness.state == expected_harness_state, session
            if expected_harness_state == harness_host_pb2.HarnessAttachment.STATE_DETACHED:
                assert not any(session_id in process.assigned_session_ids for process in status.processes), status
            return session
    raise AssertionError("HarnessHost did not publish the requested Session status")


async def test_routed_interrupt_cannot_cross_quiesce_history_seal(
    gateway_service: GatewayProcess,
    dispatcher: SessionDispatcherClient,
    runtime_client: SandboxControlClient,
    history_service: GoHistoryService,
    sandbox: SandboxCluster,
    config: RunnerTestConfig,
    start_runtime: Callable[[str], Awaitable[tuple[RunnerProcess, str]]],
    model: ScriptedModel,
    spec: session_pb2.SessionSpec,
) -> None:
    process, original_boot_id = await start_runtime(sandbox.pod_uid)
    session_id = str(uuid4())
    await dispatcher.unary(
        dispatcher.stub.CreateSession, session_dispatcher_pb2.CreateSessionRequest(session_id=session_id, spec=spec)
    )
    await accepted_session(dispatcher, UUID(session_id))

    seed_id = str(uuid4())
    seed = await dispatcher.submit_input(session_id, seed_id, sandbox.name, sandbox.uid, "Reply SEAL-SEED")
    seed_admission = (await admitted(dispatcher, seed)).admission
    seed_request = await model.request()
    assert seed_request.user_texts[-1] == "Reply SEAL-SEED"
    await model.reply(seed_request, Text("SEED COMPLETE"))
    seed_entries = await _recorded_turn(history_service, session_id, seed_admission.cursor - 1)
    assert [
        entry.event.item_completed.text
        for entry in events.of_kind(seed_entries, "item_completed")
        if entry.event.item_completed.HasField("text")
    ] == ["SEED COMPLETE"]

    active_id, delayed_id = str(uuid4()), str(uuid4())
    active = await dispatcher.submit_input(session_id, active_id, sandbox.name, sandbox.uid, "Wait for teardown")
    active_admission = (await admitted(dispatcher, active)).admission
    active_request = await model.request()
    assert active_request.user_texts[-1] == "Wait for teardown"
    retained_session = await _wait_for_session_status(
        runtime_client, sandbox, session_id, sandbox.pod_uid, original_boot_id
    )
    assert retained_session.environment_hosted.harness.state == harness_host_pb2.HarnessAttachment.STATE_ATTACHED
    initial_snapshot = await history_snapshot(history_service.reader, session_id)
    assert initial_snapshot.spec.cwd == spec.cwd
    assert initial_snapshot.spec.model == spec.model
    assert initial_snapshot.spec.placement == spec.placement
    assert spec.instructions in initial_snapshot.spec.instructions
    await model.hold(active_request)
    active_entries = await _recorded_turn(
        history_service, session_id, active_admission.cursor - 1, events.is_kind("harness_user_message_confirmed")
    )
    active_turn_id = events.of_kind(active_entries, "harness_user_message_confirmed")[
        -1
    ].event.harness_user_message_confirmed.turn_id

    gate = config.state_dir / _GATE_DIRECTORY
    # This distinct, real controller caller crosses the public Gateway and waits for Runtime's
    # close-admission -> terminate-harness -> seal-journal -> History acknowledgement barrier.
    controller = gateway_service.environment_runtime_client(
        "spiffe://agentplane/sandbox-controller", request_timeout_s=30
    )
    delayed_call: asyncio.Task[harness_host_pb2.InterruptTurnResponse] | None = None
    diagnostic_dir = undeclared_outputs_dir() / "quiesce-cutoff"
    try:
        await asyncio.to_thread(gate.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread((gate / "command").write_text, delayed_id)
        delayed_call = asyncio.create_task(
            gateway_service.environment_runtime_client("spiffe://agentplane/sessions").harness_host_unary(
                sandbox.destination,
                lambda host: host.InterruptTurn,
                harness_host_pb2.InterruptTurnRequest(
                    session_id=session_id, command_id=delayed_id, turn_id=active_turn_id
                ),
                expected_incarnation_uid=sandbox.pod_uid,
            )
        )
        await _wait_for_file(gate / "entered", delayed_id, task=delayed_call)

        await controller.runner_unary(sandbox.destination, lambda stub: stub.Quiesce, session_pb2.QuiesceRequest())
        await _wait_for_quiesced(runtime_client, sandbox, sandbox.pod_uid, original_boot_id)

        sealed = await _history_page(history_service, session_id)
        assert sealed.HasField("feed_state")
        assert sealed.feed_state.HasField("sealed")
        sealed_entries = list(sealed.entries)
        sealed_cursor = sealed.feed_state.sealed.cursor
        assert sealed_entries
        assert sealed_entries[-1].cursor == sealed_cursor
        events.assert_contiguous(sealed_entries)
        assert [
            entry.event.command_admitted.command.command_id
            for entry in events.of_kind(sealed_entries, "command_admitted")
        ] == [seed_id, active_id]
        (interrupted,) = [
            entry
            for entry in events.of_kind(sealed_entries, "turn_completed")
            if entry.event.turn_completed.turn_id == active_turn_id
        ]
        assert interrupted.event.turn_completed.status == event_pb2.TURN_STATUS_INTERRUPTED
        assert interrupted.event.turn_completed.ended_by_teardown
        assert any(
            entry.event.harness_exited.stopped_by_runner for entry in events.of_kind(sealed_entries, "harness_exited")
        )

        # The held call already passed Environments routing and the Session service-SVID check. Only
        # now does it enter the production HarnessHost handler; the interceptor records its status.
        await asyncio.to_thread((gate / "release").write_text, "release")
        with pytest.raises(RefusedError):
            await asyncio.wait_for(delayed_call, timeout=10)
        await _wait_for_file(gate / "result", "FailedPrecondition")

        after = await _history_page(history_service, session_id)
        assert [entry.SerializeToString(deterministic=True) for entry in after.entries] == [
            entry.SerializeToString(deterministic=True) for entry in sealed_entries
        ]
        assert after.feed_state.SerializeToString(deterministic=True) == sealed.feed_state.SerializeToString(
            deterministic=True
        )
        assert after.feed_state.sealed.cursor == sealed_cursor
        assert [
            entry.event.item_completed.text
            for entry in events.of_kind(after.entries, "item_completed")
            if entry.event.item_completed.HasField("text")
        ] == ["SEED COMPLETE"]
        assert delayed_id not in {
            entry.event.command_admitted.command.command_id
            for entry in events.of_kind(after.entries, "command_admitted")
        }
        assert model.request_count == 2

        # The old process is durably sealed. Only a real, new Pod UID and Controller binding may
        # adopt that seal; the same Session journal then accepts fresh Dispatcher input.
        await process.stop()
        await process.close()
        successor_uid = await sandbox.replace_runner_pod()
        assert successor_uid != sandbox.pod_uid
        _, successor_boot_id = await start_runtime(successor_uid)
        assert successor_boot_id != original_boot_id
        await _wait_for_session_status(
            runtime_client,
            sandbox,
            session_id,
            successor_uid,
            successor_boot_id,
            expected_harness_state=harness_host_pb2.HarnessAttachment.STATE_DETACHED,
        )

        successor_id = str(uuid4())
        successor = await dispatcher.submit_input(
            session_id, successor_id, sandbox.name, sandbox.uid, "Reply after successor adoption"
        )
        successor_admission = (await admitted(dispatcher, successor)).admission
        successor_request = await model.request()
        assert successor_request.user_texts[-1] == "Reply after successor adoption"
        await model.reply(successor_request, Text("SUCCESSOR COMPLETE"))
        await _recorded_turn(history_service, session_id, successor_admission.cursor - 1)

        resumed_page = await _history_page(history_service, session_id)
        resumed_entries = list(resumed_page.entries)
        assert [entry.SerializeToString(deterministic=True) for entry in resumed_entries[: len(sealed_entries)]] == [
            entry.SerializeToString(deterministic=True) for entry in sealed_entries
        ]
        events.assert_contiguous(resumed_entries)
        assert [
            entry.event.command_admitted.command.command_id
            for entry in events.of_kind(resumed_entries, "command_admitted")
        ] == [seed_id, active_id, successor_id]
        (reopened,) = events.of_kind(resumed_entries, "journal_reopened")
        assert reopened.cursor == sealed_cursor + 1
        assert reopened.event.journal_reopened.incarnation_uid == successor_uid
        assert reopened.cursor < successor_admission.cursor
        assert len(events.of_kind(resumed_entries, "turn_completed")) == 3
        assert [
            entry.event.item_completed.text
            for entry in events.of_kind(resumed_entries, "item_completed")
            if entry.event.item_completed.HasField("text")
        ] == ["SEED COMPLETE", "SUCCESSOR COMPLETE"]
        assert delayed_id not in {
            entry.event.command_admitted.command.command_id
            for entry in events.of_kind(resumed_entries, "command_admitted")
        }
        successor_snapshot = await history_snapshot(history_service.reader, session_id)
        assert successor_snapshot.spec == initial_snapshot.spec
        assert successor_snapshot.incarnation_uid == successor_uid
        assert model.request_count == 3
    finally:
        if gate.exists() and not await asyncio.to_thread((gate / "release").exists):
            await asyncio.to_thread((gate / "release").write_text, "release")
        if delayed_call is not None and not delayed_call.done():
            with suppress(Exception):
                await asyncio.wait_for(delayed_call, timeout=10)
        elif delayed_call is not None:
            with suppress(Exception):
                delayed_call.result()
        if gate.exists():
            await asyncio.to_thread(_archive_gate, gate, diagnostic_dir)
