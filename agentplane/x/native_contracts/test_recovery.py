"""Bounded contract pilot: real Gateway, Runtime, History and durable stores."""

import asyncio
import os
import shutil
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from uuid import uuid4

import grpc
import pytest
import pytest_bazel
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.env.config import Endpoint, EnvironmentRuntimeControl
from agentplane.env.e2e.testing import events, launches
from agentplane.env.e2e.testing.fixtures import ModelEndpoint
from agentplane.env.e2e.testing.model_config import model_config
from agentplane.env.e2e.testing.process_fixtures import RunnerProcess, pid1_container
from agentplane.grpc.calls import ServiceError
from agentplane.history_service.testing.go_service import GoHistoryService
from agentplane.proto import event_log_pb2, session_pb2
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient, accepted_session, admitted
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.testing.scripted_model import ScriptedModel, Text

# gazelle:include_dep @pypi//protobuf


async def committed_turn(history: GoHistoryService, session_id: str, after: int) -> list[event_log_pb2.EventEntry]:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(20), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            entries = list(
                (await history.reader.read_session_events(session_id, after_cursor=after, limit=1000)).entries
            )
            assert any(events.turn_completed(entry) for entry in entries)
            return entries
    raise AssertionError("History did not commit turn completion")


async def test_runtime_transport_outage_retains_history_and_accepts_next_input(
    gateway_service: GatewayProcess,
    dispatcher: SessionDispatcherClient,
    client_token: Path,
    history_service: GoHistoryService,
    sandbox: SandboxCluster,
    control: EnvironmentRuntimeControl,
    ingress: Endpoint,
    endpoint: ModelEndpoint,
    model: ScriptedModel,
    spec: session_pb2.SessionSpec,
    tmp_path: Path,
) -> None:
    config = launches.config(
        session_pb2.HARNESS_CLAUDE,
        endpoint.origin,
        sandbox=sandbox.identity,
        state_dir=tmp_path / "state",
        home=tmp_path / "home",
    )
    environment = {**config.harness_environment, "ANTHROPIC_AUTH_TOKEN": launches.TOKEN, "OTEL_SDK_DISABLED": "true"}
    remote = gateway_service.gateway_client(client_token, request_timeout_s=2)
    async with model_config() as model_endpoint, AsyncExitStack() as runtime_stack:
        command = launches.command(config, control=control, ingress=ingress, model_config_target=model_endpoint)

        @asynccontextmanager
        async def runtime() -> AsyncIterator[None]:
            container = await asyncio.to_thread(pid1_container, command, tmp_path, environment)
            await asyncio.to_thread(container.__enter__)
            process = RunnerProcess(container)
            try:
                await gateway_service.wait_for_runner(remote, sandbox.destination)
                yield
            finally:
                try:
                    await process.stop()
                finally:
                    await process.close()

        session_id = uuid4()
        await runtime_stack.enter_async_context(runtime())
        await dispatcher.unary(
            dispatcher.stub.CreateSession,
            session_dispatcher_pb2.CreateSessionRequest(session_id=str(session_id), spec=spec),
        )
        await accepted_session(dispatcher, session_id)
        first_id = str(uuid4())
        first = await dispatcher.submit_input(str(session_id), first_id, sandbox.name, sandbox.uid, "Reply FIRST")
        first_receipt = (await admitted(dispatcher, first)).admission
        await model.reply(await model.request(), Text("FIRST"))
        before = await committed_turn(history_service, str(session_id), first_receipt.cursor - 1)

        # Remove the real Runtime listener. Calls must fail through the real Gateway;
        # serving retained History must remain independent of this transport.
        await runtime_stack.aclose()
        with pytest.raises(ServiceError) as unavailable:
            await remote.runner(sandbox.destination).status()
        assert unavailable.value.code == grpc.StatusCode.UNAVAILABLE
        retained = await history_service.reader.read_session_events(
            str(session_id), after_cursor=first_receipt.cursor - 1, limit=1000
        )
        assert list(retained.entries) == before

        if os.environ.get("NATIVE_CONTRACT_DISCARD_RUNTIME_JOURNAL") == "1":
            shutil.rmtree(config.state_dir)
        await runtime_stack.enter_async_context(runtime())
        assert str(session_id) in {row.session_id for row in await remote.list_sessions(sandbox.destination)}
        # Replay the durable Dispatcher receipt, then send new input through Runtime.
        replay = await dispatcher.submit_input(str(session_id), first_id, sandbox.name, sandbox.uid, "Reply FIRST")
        assert (await admitted(dispatcher, replay)).admission == first_receipt
        second_id = str(uuid4())
        second = await dispatcher.submit_input(str(session_id), second_id, sandbox.name, sandbox.uid, "Reply SECOND")
        second_receipt = (await admitted(dispatcher, second)).admission
        await model.reply(await model.request(), Text("SECOND"))
        after = await committed_turn(history_service, str(session_id), second_receipt.cursor - 1)
        all_entries = list((await history_service.reader.read_session_events(str(session_id), limit=1000)).entries)
        assert [
            row.event.command_admitted.command.command_id for row in events.of_kind(all_entries, "command_admitted")
        ] == [first_id, second_id]
        assert len(events.of_kind(all_entries, "turn_completed")) == 2
        assert any(events.turn_completed(entry) for entry in after)


if __name__ == "__main__":
    pytest_bazel.main()
