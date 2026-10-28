"""The generated Runner client against the Go runner and both native harnesses."""

import asyncio
import shlex
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import grpc
import pytest
import pytest_bazel
from more_itertools import one
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.grpc.calls import ServiceError
from agentplane.history_service.testing.go_service import wait_for_history_session
from agentplane.proto import command_pb2, event_log_pb2, runner_pb2
from agentplane.proto.sandboxes_pb2 import SandboxDestination
from agentplane.runner import runner_service_pb2
from agentplane.runner.testing import events, launches
from agentplane.runner.testing.config import RunnerTestConfig
from agentplane.runner.testing.process import RunningRunner
from agentplane.runner_gateway.client import SandboxControlClient, SandboxRunner
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.testing.scripted_model import ScriptedModel, Text

# gazelle:include_dep @pypi//protobuf


@pytest.fixture
def bootstrap(workspace: Path) -> str | None:
    """The Sandbox's bootstrap, which the runner runs before its first Session."""
    return f"printf B >> {shlex.quote(str(workspace / 'bootstrap-runs'))}"


@pytest.fixture
def config(config: RunnerTestConfig, bootstrap: str | None) -> RunnerTestConfig:
    return replace(config, launch=replace(config.launch, bootstrap=bootstrap))


@pytest.fixture
def remote(sandbox_control: SandboxControlClient, runner: RunningRunner) -> SandboxControlClient:
    """The app-authenticated Runner client routed through the deployed Go Gateway."""
    return sandbox_control


async def inspect(runner: SandboxRunner, session_id: str) -> runner_pb2.SessionSummary:
    return one(summary for summary in await runner.list_sessions() if summary.session_id == session_id)


async def submit_input(
    remote: SandboxControlClient, destination: SandboxDestination, session_id: str, command: command_pb2.Command
) -> event_log_pb2.EventEntry:
    assert command.WhichOneof("operation") == "submit_input"
    response: runner_service_pb2.SubmitInputResponse = await remote.runner_unary(
        destination,
        lambda stub: stub.SubmitInput,
        runner_service_pb2.SubmitInputRequest(
            session_id=session_id, command_id=command.command_id, text=command.submit_input.text
        ),
        timeout_s=remote.request_timeout_s,
    )
    return response.admission


async def complete_turn(
    remote: SandboxControlClient,
    local: RunningRunner,
    destination: SandboxDestination,
    model: ScriptedModel,
    session_id: str,
    command_id: str,
) -> list[event_log_pb2.EventEntry]:
    command = command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text="Reply: GRPC_OK"))
    receipt = await submit_input(remote, destination, session_id, command)
    assert receipt.event.command_admitted.command == command
    assert receipt.origin.source_id
    assert receipt.origin.sequence > 0
    # Neither admission nor exact receipt replay waits for model completion.
    assert await submit_input(remote, destination, session_id, command) == receipt
    request = await model.request()
    assert launches.PLATFORM_INSTRUCTIONS in request.system_text
    assert "Test task guidance." in request.system_text
    assert launches.SANDBOX.uid in request.system_text
    await model.reply(request, Text("GRPC_OK"))
    entries = await wait_for_entries(
        local, session_id, receipt.cursor - 1, lambda rows: any(events.turn_completed(row) for row in rows)
    )
    assert events.of_kind(entries, "command_admitted") == [receipt]
    assert any(
        command.command_id in e.event.harness_user_message_confirmed.origin_command_ids
        for e in events.of_kind(entries, "harness_user_message_confirmed")
    )
    return entries


async def wait_for_entries(
    runner: RunningRunner, session_id: str, after: int, complete: Callable[[list[event_log_pb2.EventEntry]], bool]
) -> list[event_log_pb2.EventEntry]:
    """Wait for committed entries through History's authenticated reader and change watch."""
    entries: list[event_log_pb2.EventEntry] = []
    cursor = after
    watch = None
    try:
        async with asyncio.timeout(20):
            while True:
                try:
                    current = await runner.history.read_session_events(session_id, after_cursor=0, limit=1)
                except ServiceError as error:
                    if error.code != grpc.StatusCode.NOT_FOUND:
                        raise
                    await wait_for_history_session(runner.history, session_id)
                    continue
                # A control response can name an admission cursor before History commits it.
                # Start the raw watch from the current History prefix and filter older events below.
                cursor = min(cursor, current.last_cursor)
                new_entries = await runner.history_entries(session_id, cursor)
                entries.extend(entry for entry in new_entries if entry.cursor > after)
                if complete(entries):
                    return entries
                if new_entries:
                    cursor = new_entries[-1].cursor
                if watch is None:
                    watch = runner.history.follow_session(session_id=session_id, after_cursor=cursor)
                try:
                    changed = await anext(watch)
                except StopAsyncIteration:
                    await watch.aclose()
                    watch = None
                    continue
                if changed.HasField("reconnect_required"):
                    await watch.aclose()
                    watch = None
                    continue
                if changed.HasField("caught_up") and changed.through_cursor <= cursor:
                    continue
    finally:
        if watch is not None:
            await watch.aclose()


async def test_launch_delivery_and_restart_preserve_evidence_and_configuration(
    remote: SandboxControlClient,
    sandbox: SandboxCluster,
    gateway_service: GatewayProcess,
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    workspace: Path,
    runner: RunningRunner,
) -> None:
    bootstrap_marker = workspace / "bootstrap-runs"
    setup_marker = workspace / "setup-runs"
    spec.instructions = "Test task guidance."
    launch_spec = runner_pb2.SessionSpec()
    launch_spec.CopyFrom(spec)
    setup_started_marker = workspace / "setup-started"
    setup_release_marker = workspace / "setup-release"
    setup_script = (
        f"printf started > {shlex.quote(str(setup_started_marker))}; "
        f"while [ ! -e {shlex.quote(str(setup_release_marker))} ]; do sleep 0.01; done; "
        f"printf S >> {shlex.quote(str(setup_marker))}"
    )
    destination = remote.runner(sandbox.destination)
    requested = uuid4()
    created = await destination.open_session(session_id=requested, spec=launch_spec, setup_script=setup_script)
    session = created.session_id
    assert not (await destination.status()).processes
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(15), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            assert setup_started_marker.exists()
    opened = await inspect(destination, session)
    assert opened.setup_state == runner_pb2.SETUP_STATE_RUNNING
    status = await destination.status()
    assert not status.processes
    (preparing,) = [row for row in status.sessions if row.session_id == session]
    assert preparing.execution_state == runner_pb2.SESSION_EXECUTION_STATE_PREPARING
    setup_release_marker.touch()
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(15), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            opened = await inspect(destination, session)
            assert opened.setup_state == runner_pb2.SETUP_STATE_SUCCEEDED
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
    status = await destination.status()
    assert not status.processes
    (dormant,) = [row for row in status.sessions if row.session_id == session]
    assert dormant.execution_state == runner_pb2.SESSION_EXECUTION_STATE_DORMANT
    entries = await complete_turn(remote, runner, sandbox.destination, model, session, "grpc-notice")
    (started,) = events.of_kind(entries, "harness_started")
    assert not started.event.harness_started.resumed
    active = await destination.status()
    (active_session,) = [row for row in active.sessions if row.session_id == session]
    native_session_id = active_session.native_session_id
    assert native_session_id
    assert len(active.processes) == 1
    await runner.restart()
    await wait_for_entries(
        runner, session, entries[-1].cursor, lambda tail: any(events.kind(entry) == "harness_exited" for entry in tail)
    )
    assert (await inspect(destination, session)).harness_state == runner_pb2.HARNESS_STATE_STOPPED
    stopped = await destination.status()
    assert not stopped.processes
    (stopped_session,) = [row for row in stopped.sessions if row.session_id == session]
    assert stopped_session.native_session_id == native_session_id
    assert stopped_session.execution_state == runner_pb2.SESSION_EXECUTION_STATE_DORMANT
    # The restarted process reconnects through the Gateway before the next routed call.
    await gateway_service.wait_for_runner(remote, sandbox.destination)
    resumed_entries = await complete_turn(remote, runner, sandbox.destination, model, session, "grpc-resumed")
    destination = remote.runner(sandbox.destination)
    resumed_starts = events.of_kind(resumed_entries, "harness_started")
    assert len(resumed_starts) == 1
    assert resumed_starts[0].event.harness_started.resumed
    resumed = await destination.status()
    (resumed_session,) = [row for row in resumed.sessions if row.session_id == session]
    assert resumed_session.native_session_id == native_session_id
    assert len(resumed.processes) == 1
    assert (await destination.list_sessions())[0].spec == opened.spec
    invalid_spec = runner_pb2.SessionSpec()
    invalid_spec.CopyFrom(launch_spec)
    invalid_spec.model = ""
    with pytest.raises(ServiceError) as invalid:
        await destination.open_session(session_id=uuid4(), spec=invalid_spec)
    assert invalid.value.code == grpc.StatusCode.INVALID_ARGUMENT
    assert bootstrap_marker.read_text() == "B"
    assert setup_marker.read_text() == "S"


async def test_runner_command_does_not_create_unknown_session(
    remote: SandboxControlClient, sandbox: SandboxCluster
) -> None:
    runner = remote.runner(sandbox.destination)
    unknown = str(uuid4())
    with pytest.raises(ServiceError) as commanded:
        await submit_input(
            remote,
            sandbox.destination,
            unknown,
            command_pb2.Command(command_id="unknown-notice", submit_input=command_pb2.SubmitInput(text="no creation")),
        )
    assert commanded.value.code == grpc.StatusCode.NOT_FOUND
    assert not await runner.list_sessions()


async def test_failed_setup_reports_command_failure(
    remote: SandboxControlClient, sandbox: SandboxCluster, runner: RunningRunner, spec: runner_pb2.SessionSpec
) -> None:
    destination = remote.runner(sandbox.destination)
    created = await destination.open_session(session_id=uuid4(), spec=spec, setup_script="exit 42")
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(15), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            opened = await inspect(destination, created.session_id)
            assert opened.setup_state != runner_pb2.SETUP_STATE_RUNNING
    assert opened.setup_state == runner_pb2.SETUP_STATE_FAILED
    receipt = await submit_input(
        remote,
        sandbox.destination,
        created.session_id,
        command_pb2.Command(command_id="after-failed-setup", submit_input=command_pb2.SubmitInput(text="not run")),
    )
    assert receipt.event.HasField("command_admitted")
    failures = await wait_for_entries(
        runner,
        created.session_id,
        receipt.cursor,
        lambda entries: any(events.kind(entry) == "command_failed" for entry in entries),
    )
    (failure,) = [entry for entry in failures if events.kind(entry) == "command_failed"]
    assert failure.event.command_failed.command_id == "after-failed-setup"
    assert "setup" in failure.event.command_failed.reason


async def test_invalid_launch_never_creates(remote: SandboxControlClient, sandbox: SandboxCluster) -> None:
    runner = remote.runner(sandbox.destination)
    invalid_specs = [runner_pb2.SessionSpec(), runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CODEX, model="m")]
    for invalid in invalid_specs:
        with pytest.raises(ServiceError) as rejected:
            await runner.open_session(session_id=uuid4(), spec=invalid)
        assert rejected.value.code == grpc.StatusCode.INVALID_ARGUMENT
    assert not await runner.list_sessions()


@pytest.mark.parametrize("bootstrap", ["exit 42"])
async def test_failed_bootstrap_reports_failure_when_input_arrives(
    remote: SandboxControlClient, sandbox: SandboxCluster, runner: RunningRunner, spec: runner_pb2.SessionSpec
) -> None:
    destination = remote.runner(sandbox.destination)
    opened = await destination.open_session(session_id=uuid4(), spec=spec)
    status = await destination.status()
    assert not status.processes
    (session,) = [row for row in status.sessions if row.session_id == opened.session_id]
    assert session.execution_state == runner_pb2.SESSION_EXECUTION_STATE_DORMANT

    admission = await submit_input(
        remote,
        sandbox.destination,
        opened.session_id,
        command_pb2.Command(command_id="after-failed-bootstrap", submit_input=command_pb2.SubmitInput(text="not run")),
    )
    entries = await wait_for_entries(
        runner,
        opened.session_id,
        admission.cursor,
        lambda tail: (
            any(events.kind(entry) == "harness_launch_failed" for entry in tail)
            and any(events.kind(entry) == "command_failed" for entry in tail)
        ),
    )
    (launch_failure,) = [entry for entry in entries if events.kind(entry) == "harness_launch_failed"]
    (command_failure,) = [entry for entry in entries if events.kind(entry) == "command_failed"]
    assert "bootstrap failed" in launch_failure.event.harness_launch_failed.reason
    assert command_failure.event.command_failed.command_id == "after-failed-bootstrap"
    assert not (await destination.status()).processes
    assert {row.session_id for row in await destination.list_sessions()} == {opened.session_id}


if __name__ == "__main__":
    pytest_bazel.main()
