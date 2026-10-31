"""The app's Runner Gateway and History Service path against authenticated gRPC peers."""

from uuid import UUID, uuid4

import httpx
import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict, ParseDict
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.testing.dispatcher_client import SessionDispatcherClient
from agentplane.app.testing.history import SessionHistory
from agentplane.app.testing.runner_gateway import admitted, delivered_opening
from agentplane.grpc.calls import ServiceError
from agentplane.history_service import history_service_pb2
from agentplane.proto import command_pb2, runner_pb2
from agentplane.runner import runner_service_pb2
from agentplane.runner.testing import launches
from agentplane.runner.testing.process import RunningRunner
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess
from agentplane.sandboxes.testing.kubernetes import SANDBOX, SandboxCluster
from agentplane.testing.scripted_model import ScriptedModel, Text

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//grpcio


async def test_production_bridge_reads_runner_history_across_gateway_restarts(
    sandbox: SandboxCluster,
    gateway_service: GatewayProcess,
    sandbox_control: SandboxControlClient,
    session_dispatcher: SessionDispatcherClient,
    event_logs: SessionHistory,
    http: httpx.AsyncClient,
    runner: RunningRunner,
    spec: runner_pb2.SessionSpec,
    model: ScriptedModel,
) -> None:
    response = await http.post(
        f"/sandboxes/{SANDBOX}/sessions", json={"session_id": str(uuid4()), "spec": MessageToDict(spec)}
    )
    assert response.status_code == 201, response.text
    opening = ParseDict(response.json(), session_dispatcher_pb2.SessionOpening())
    await delivered_opening(session_dispatcher, UUID(opening.session_id))
    (runner_session,) = await sandbox_control.runner(sandbox.destination).list_sessions()
    assert runner_session.session_id == opening.session_id
    assert launches.PLATFORM_INSTRUCTIONS in runner_session.spec.instructions
    thread = UUID(opening.session_id)
    runner_client = sandbox_control.runner(sandbox.destination)
    status = await runner_client.status()
    assert not status.processes
    (dormant,) = [row for row in status.sessions if row.session_id == str(thread)]
    assert dormant.execution_state == runner_pb2.SESSION_EXECUTION_STATE_DORMANT
    first = command_pb2.Command(command_id="remote-first", submit_input=command_pb2.SubmitInput(text="first"))
    receipt = await admitted(
        session_dispatcher,
        (
            await session_dispatcher.unary(
                session_dispatcher.stub.SubmitInput,
                session_dispatcher_pb2.SubmitInputRequest(
                    command_id=first.command_id,
                    session_id=str(thread),
                    sandbox=SANDBOX,
                    sandbox_uid=sandbox.uid,
                    text=first.submit_input.text,
                ),
            )
        ).dispatch,
    )
    assert receipt.admission.event.command_admitted.command == first
    # The runner receipt can arrive before the independent app archive copy.
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            assert receipt.admission in await event_logs.events(thread, limit=1000)
    await model.reply(await model.request(), Text("FIRST"))
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            first_turn = await runner.history_entries(str(thread))
            assert any(entry.event.WhichOneof("observation") == "turn_completed" for entry in first_turn)
    summary = await event_logs.reader.get_session_summary(str(thread))
    assert summary.feed_status == history_service_pb2.FEED_STATUS_ACTIVE
    active = await runner_client.status()
    (active_session,) = [row for row in active.sessions if row.session_id == str(thread)]
    native_session_id = active_session.native_session_id
    assert native_session_id
    assert len(active.processes) == 1
    starts = [entry for entry in first_turn if entry.event.WhichOneof("observation") == "harness_started"]
    assert starts[-1].event.harness_started.resumed is False
    await runner.restart()
    await gateway_service.wait_for_runner(sandbox_control, sandbox.destination)
    stopped = await runner_client.status()
    assert not stopped.processes
    (stopped_session,) = [row for row in stopped.sessions if row.session_id == str(thread)]
    assert stopped_session.execution_state == runner_pb2.SESSION_EXECUTION_STATE_DORMANT
    assert stopped_session.native_session_id == native_session_id
    second = command_pb2.Command(command_id="remote-second", submit_input=command_pb2.SubmitInput(text="second"))
    next_receipt = await admitted(
        session_dispatcher,
        (
            await session_dispatcher.unary(
                session_dispatcher.stub.SubmitInput,
                session_dispatcher_pb2.SubmitInputRequest(
                    command_id=second.command_id,
                    session_id=str(thread),
                    sandbox=SANDBOX,
                    sandbox_uid=sandbox.uid,
                    text=second.submit_input.text,
                ),
            )
        ).dispatch,
    )
    assert next_receipt.admission.cursor > receipt.admission.cursor
    assert next_receipt.admission.origin.source_id == receipt.admission.origin.source_id
    await model.reply(await model.request(), Text("SECOND"))
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            history_entries = await runner.history_entries(str(thread))
            starts = [entry for entry in history_entries if entry.event.WhichOneof("observation") == "harness_started"]
            assert len(starts) == 2
            assert starts[0].event.harness_started.resumed is False
            assert starts[1].event.harness_started.resumed is True
            second_turn = [
                entry
                for entry in history_entries
                if entry.event.WhichOneof("observation") == "turn_completed"
                and entry.cursor > next_receipt.admission.cursor
            ]
            assert second_turn
    resumed = await runner_client.status()
    (resumed_session,) = [row for row in resumed.sessions if row.session_id == str(thread)]
    assert resumed_session.native_session_id == native_session_id
    assert len(resumed.processes) == 1
    await runner.restart()
    await gateway_service.wait_for_runner(sandbox_control, sandbox.destination)
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(15), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            history_entries = await runner.history_entries(str(thread))
            archived = await event_logs.events(thread, limit=1000)
            assert archived == history_entries
            assert any(entry.event.HasField("harness_exited") for entry in archived)


async def test_runner_client_does_not_fall_back_after_gateway_stops(
    sandbox: SandboxCluster,
    sandbox_control: SandboxControlClient,
    gateway_service: GatewayProcess,
    runner: RunningRunner,
) -> None:
    assert await sandbox_control.runner(sandbox.destination).list_sessions() == []
    await gateway_service.stop()
    with pytest.raises(ServiceError):
        await sandbox_control.runner(sandbox.destination).list_sessions()


async def test_runner_proxy_lists_and_sends_to_a_dormant_session(
    sandbox: SandboxCluster,
    gateway_service: GatewayProcess,
    sandbox_control: SandboxControlClient,
    session_dispatcher: SessionDispatcherClient,
    runner: RunningRunner,
    spec: runner_pb2.SessionSpec,
    model: ScriptedModel,
) -> None:
    session_id = str(uuid4())
    await sandbox_control.runner_unary(
        sandbox.destination,
        lambda stub: stub.OpenSession,
        runner_service_pb2.OpenSessionRequest(session_id=session_id, spec=spec),
        timeout_s=sandbox_control.lifecycle_timeout_s,
    )
    await sandbox_control.runner_unary(
        sandbox.destination,
        lambda stub: stub.SubmitInput,
        runner_service_pb2.SubmitInputRequest(session_id=session_id, command_id="first", text="hi"),
    )
    await model.reply(await model.request(), Text("hello"))
    client = sandbox_control.runner(sandbox.destination)
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            (summary,) = await client.list_sessions()
            assert summary.session_id == session_id
            assert not summary.active_turn_id
    status = await client.status()
    (active,) = [row for row in status.sessions if row.session_id == session_id]
    native_session_id = active.native_session_id
    assert native_session_id
    assert len(status.processes) == 1
    # This exercises orderly runner shutdown and recovery. Killing the harness
    # directly is a process-loss failure, which is reported as FAILED.
    await runner.restart()
    await gateway_service.wait_for_runner(sandbox_control, sandbox.destination)
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            (summary,) = await client.list_sessions()
            assert summary.harness_state == runner_pb2.HARNESS_STATE_STOPPED
    status = await client.status()
    assert not status.processes
    (dormant,) = [row for row in status.sessions if row.session_id == session_id]
    assert dormant.execution_state == runner_pb2.SESSION_EXECUTION_STATE_DORMANT
    assert dormant.native_session_id == native_session_id

    receipt = await admitted(
        session_dispatcher,
        (
            await session_dispatcher.unary(
                session_dispatcher.stub.SubmitInput,
                session_dispatcher_pb2.SubmitInputRequest(
                    session_id=session_id,
                    command_id="wake-on-input",
                    sandbox=SANDBOX,
                    sandbox_uid=sandbox.uid,
                    text="again",
                ),
            )
        ).dispatch,
    )
    assert receipt.admission.event.command_admitted.command.command_id == "wake-on-input"
    await model.reply(await model.request(), Text("resumed"))
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            entries = await runner.history_entries(session_id)
            starts = [entry for entry in entries if entry.event.WhichOneof("observation") == "harness_started"]
            assert len(starts) == 2
            assert starts[0].event.harness_started.resumed is False
            assert starts[1].event.harness_started.resumed is True
            assert any(
                entry.event.WhichOneof("observation") == "turn_completed" and entry.cursor > receipt.admission.cursor
                for entry in entries
            )
    status = await client.status()
    (resumed,) = [row for row in status.sessions if row.session_id == session_id]
    assert resumed.native_session_id == native_session_id
    assert len(status.processes) == 1
    summaries = await client.list_sessions()
    assert [summary.session_id for summary in summaries] == [str(session_id)]


if __name__ == "__main__":
    pytest_bazel.main()
