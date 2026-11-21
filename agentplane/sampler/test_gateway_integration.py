"""Real central sampling through Gateway, Runner, exec-server and authenticated History."""

import asyncio
import base64
import io
import json
import logging
import os
import signal
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict
from PIL import Image

from agentplane.env import config as configuration
from agentplane.env.e2e.testing import launches
from agentplane.env.e2e.testing.gateway import serve_gateway
from agentplane.env.e2e.testing.history_tunnel import history_tunnel
from agentplane.env.e2e.testing.process import RunningRunner
from agentplane.env.runtime.execution import executor_pb2, executor_pb2_grpc
from agentplane.env.runtime.execution.testing.codex_exec_server_container import (
    CodexExecServerContainer,
    inspect_codex_exec_server_container,
)
from agentplane.grpc.calls import dial
from agentplane.harness_host import harness_host_pb2
from agentplane.history_service.testing.go_service import go_history_service, migrate_history_database
from agentplane.native.codex.testing.responses_sse import CustomToolCall, Message, response_stream
from agentplane.net.peertls.config import CENTRAL_SAMPLER_URI, MTLS, SANDBOX_CONTROLLER_URI
from agentplane.proto import event_pb2, session_pb2
from agentplane.sampler.api import sampler_admin_pb2
from agentplane.sampler.testing import Endpoint, create_request, go_sampler, settings, turn_completed
from agentplane.sampler.testing_store import Store
from agentplane.sandboxes.testing.kubernetes import SandboxCluster, sandbox_cluster
from agentplane.session_dispatcher.testing.gateway_process import run_gateway
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.electric_role import ensure_electric_role
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from testing.postgres import create_temporary_database
from testing.sqlalchemy_engines import postgres_async_engine

# gazelle:include_dep //agentplane/env/runtime/execution/testing:codex_exec_server_container
pytest_plugins = ("agentplane.env.runtime.execution.testing.codex_exec_server_container",)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def capture_sampler_failure(store: Store, session_id: str) -> AsyncIterator[None]:
    directory = undeclared_outputs_dir()
    try:
        yield
    except BaseException:
        try:
            async with asyncio.timeout(5):
                with (directory / "sampler-events.jsonl").open("w") as output:
                    cursor = 0
                    while entries := await store.events(session_id, cursor):
                        for entry in entries:
                            output.write(json.dumps(MessageToDict(entry, preserving_proto_field_name=True)) + "\n")
                        cursor = entries[-1].cursor
                (directory / "sampler-intents.json").write_text(
                    json.dumps([asdict(intent) for intent in await store.intents()])
                )
                (directory / "sampler-state.json").write_text(json.dumps(asdict(await store.get_session(session_id))))
        except Exception:
            logger.exception("Could not collect sampler failure diagnostics")
            try:
                (directory / "sampler-diagnostics-error.txt").write_text(traceback.format_exc())
            except OSError:
                logger.exception("Could not retain sampler diagnostics collection error")
        raise


@pytest.fixture
async def sandbox(api_server: KubernetesApiServer) -> AsyncIterator[SandboxCluster]:
    async with sandbox_cluster(api_server) as sandbox:
        yield sandbox


async def test_gateway_shell_patch_and_history_survive_executor_loss(
    sandbox: SandboxCluster,
    database_url: str,
    postgres_admin_url: str,
    api_server: KubernetesApiServer,
    codex_exec_server_container: CodexExecServerContainer,
    code_mode_host: str,
    tmp_path: Path,
) -> None:
    sampler = ServiceAccountRef(namespace="test-central", name="sampler")
    ensure_electric_role(postgres_admin_url)
    with (
        create_temporary_database(postgres_admin_url, "central_gateway_" + uuid4().hex) as gateway_database,
        create_temporary_database(postgres_admin_url, "central_history_" + uuid4().hex) as history_database,
    ):
        migrate_history_database(history_database)
        async with (
            postgres_async_engine(database_url) as engine,
            Endpoint() as endpoint,
            serve_gateway(sandbox, tmp_path) as runner_gateway,
            run_gateway(
                api_server.kubeconfig,
                database_url=gateway_database,
                namespace=sandbox.namespace,
                runtime_proxy_executor_callers=(CENTRAL_SAMPLER_URI,),
            ) as gateway,
            go_history_service(
                history_database,
                api=api_server,
                directory=tmp_path,
                writer_namespaces=[sampler.namespace, sandbox.namespace],
                sampler_service_actor=CENTRAL_SAMPLER_URI,
                policy_namespace=sandbox.namespace,
                svid_ca=gateway.environment_service.ca,
            ) as history,
        ):
            store = Store(engine)
            initial_companion = await asyncio.to_thread(
                inspect_codex_exec_server_container, codex_exec_server_container.socket
            )
            assert not initial_companion.owned
            config = launches.config(
                session_pb2.HARNESS_CODEX,
                endpoint.origin,
                sandbox=sandbox.identity,
                state_dir=tmp_path / "runner-state",
                home=tmp_path / "runner-home",
                codex_executor_url=codex_exec_server_container.url,
                codex_executor_socket=codex_exec_server_container.socket,
            )
            control = configuration.EnvironmentRuntimeControl(
                listen_address="127.0.0.1:9090",
                security=MTLS(
                    tls=gateway.environment_service.issue_workload_tls(
                        sandbox.environment_runtime_identity_uri, tmp_path / "environment-runtime-transport"
                    )
                ),
            )
            async with (
                history_tunnel(history.ingress_target) as history_proxy,
                RunningRunner(
                    config,
                    runner_gateway,
                    history=history.reader,
                    ingress_endpoint=configuration.Endpoint(
                        target=history.ingress_target,
                        svid=configuration.SVIDEndpoint(
                            server_uri="spiffe://agentplane/history-ingress", proxy_address=history_proxy
                        ),
                    ),
                    control_endpoint=control,
                    observe_control=False,
                    controller_credentials=gateway.environment_service.channel_credentials(SANDBOX_CONTROLLER_URI),
                    incarnation_uid=sandbox.pod_uid,
                ) as running_runner,
            ):
                await gateway.environment_service.wait_for_runner(
                    gateway.environment_runtime_client(), sandbox.destination
                )
                sampler_settings = settings(
                    database_url,
                    endpoint.origin,
                    None,
                    "/opt/codex/bin/codex",
                    code_mode_host,
                    undeclared_outputs_dir(),
                    gateway_target=gateway.environment_service.target,
                    gateway_tls=gateway.environment_service.tls_for(CENTRAL_SAMPLER_URI),
                    history_target=history.ingress_target,
                    history_tls=history.history_tls,
                )
                request = create_request(tmp_path / "workspace")
                request.execution_identity.name = sandbox.execution_identity_name
                request.execution_identity.uid = sandbox.execution_identity_uid
                request.spec.placement.central_sampler_v1.executor_environment.name = sandbox.destination.target.name
                request.spec.placement.central_sampler_v1.executor_environment.uid = sandbox.destination.target.uid
                sampler_settings.environment_namespace = sandbox.destination.target.namespace
                request.setup_script = (
                    "printf before > artifact.txt\n"
                    "printf 'gateway setup stdout\\n'\n"
                    "printf 'gateway setup stderr\\n' >&2\n"
                )
                session_id = request.session_id
                async with (
                    go_sampler(sampler_settings, undeclared_outputs_dir()) as stub,
                    capture_sampler_failure(store, session_id),
                ):
                    with pytest.raises(grpc.aio.AioRpcError) as bearer_rejected:
                        await stub.admin.GetSession(
                            sampler_admin_pb2.GetSessionRequest(session_id=session_id),
                            metadata=(("authorization", "Bearer ignored"),),
                            timeout=10,
                        )
                    assert bearer_rejected.value.code() == grpc.StatusCode.UNAUTHENTICATED
                    assert not await store.events(session_id, 0, limit=1000)
                    with pytest.raises(grpc.aio.AioRpcError) as absent_session:
                        await stub.admin.GetSession(
                            sampler_admin_pb2.GetSessionRequest(session_id=session_id), timeout=10
                        )
                    assert absent_session.value.code() == grpc.StatusCode.NOT_FOUND
                    await stub.CreateSession(request, timeout=10)
                    await stub.SubmitInput(
                        harness_host_pb2.SubmitInputRequest(
                            session_id=session_id, command_id="shell-and-patch", text="Write, patch and read the file."
                        ),
                        timeout=10,
                    )
                    code = (
                        "text(await tools.apply_patch("
                        + json.dumps(
                            "*** Begin Patch\n*** Update File: artifact.txt\n@@\n-before\n+after\n*** End Patch"
                        )
                        + '));\ntext(await tools.exec_command({cmd:"cat artifact.txt"}));'
                        # The cell reports on the image instead of emitting it: the scripted
                        # provider accepts at most 1 MiB request bodies.
                        + '\nconst viewed = await tools.view_image({path:"noise.png"});'
                        + "\ntext(viewed.image_url.length);\ntext(viewed.image_url.slice(-64));"
                    )
                    # Incompressible pixels keep the file above the relay's 1 MiB message bound.
                    noise = io.BytesIO()
                    Image.frombytes("RGB", (640, 640), os.urandom(640 * 640 * 3)).save(noise, format="PNG")
                    assert noise.tell() > 1024 * 1024
                    noise_url = "data:application/octet-stream;base64," + base64.b64encode(noise.getvalue()).decode()
                    async with await endpoint.next() as exchange:
                        assert (tmp_path / "workspace" / "artifact.txt").read_text() == "before"
                        (tmp_path / "workspace" / "noise.png").write_bytes(noise.getvalue())
                        await exchange.send(
                            *response_stream([CustomToolCall("shell-patch", "exec", code)], model="test-model").events
                        )
                    async with await endpoint.next() as exchange:
                        outputs = [
                            item for item in exchange.request.body["input"] if item["type"] == "custom_tool_call_output"
                        ]
                        assert "after" in json.dumps(outputs)
                        assert (tmp_path / "workspace" / "artifact.txt").read_text() == "after\n"
                        assert [part["text"] for part in outputs[-1]["output"][-2:]] == [
                            str(len(noise_url)),
                            noise_url[-64:],
                        ]
                        await exchange.send(
                            *response_stream([Message("Patched artifact: after")], model="test-model").events
                        )
                    assert (
                        await turn_completed(stub, session_id, "shell-and-patch", timeout_s=20)
                    ).status == event_pb2.TURN_STATUS_COMPLETED
                    async with dial(
                        gateway.environment_service.target,
                        channel_credentials=gateway.environment_service.channel_credentials(CENTRAL_SAMPLER_URI),
                    ) as gateway_channel:
                        stale_stream = executor_pb2_grpc.ExecutorStub(gateway_channel).OpenSession(
                            executor_pb2.OpenSessionRequest(
                                scope=executor_pb2.ExecutionScope(
                                    destination=sandbox.destination,
                                    expected_incarnation_uid="stale-" + sandbox.pod_uid,
                                    session_id=session_id,
                                ),
                                request_id=str(uuid4()),
                            )
                        )
                        with pytest.raises(grpc.aio.AioRpcError) as rejected:
                            await stale_stream.read()
                        assert rejected.value.code() == grpc.StatusCode.FAILED_PRECONDITION

                    sampler_prefix_snapshot = await store.get_session(session_id)
                    await history.folded(UUID(session_id), sampler_prefix_snapshot.cursor)
                    sampler_prefix = await store.events(session_id, 0, limit=1000)
                    prefix_page = await history.reader.read_session_events(session_id, limit=1000)
                    prefix = list(prefix_page.entries)
                    assert prefix == sampler_prefix
                    assert prefix

                    outage_command_id = str(uuid4())
                    await stub.SubmitInput(
                        harness_host_pb2.SubmitInputRequest(
                            session_id=session_id,
                            command_id=outage_command_id,
                            text="Complete this input while History is unavailable.",
                        ),
                        timeout=10,
                    )
                    outage_request = await endpoint.next()

                    outage_reader = history.reader.new_client()
                    outage_reader.request_timeout_s = 0.5
                    async with outage_reader:
                        history.process.process.send_signal(signal.SIGSTOP)
                        try:
                            with pytest.raises(TimeoutError):
                                await outage_reader.read_session_events(session_id)
                            async with outage_request as exchange:
                                await exchange.send(
                                    *response_stream(
                                        [Message("This turn completed while History was unavailable.")],
                                        model="test-model",
                                    ).events
                                )
                            assert (
                                await turn_completed(stub, session_id, outage_command_id, timeout_s=20)
                            ).status == event_pb2.TURN_STATUS_COMPLETED
                            outage_snapshot = await store.get_session(session_id)
                            outage_suffix = await store.events(session_id, prefix[-1].cursor, limit=1000)
                            assert outage_suffix
                            outage_cursor = outage_suffix[-1].cursor
                            assert outage_snapshot.cursor == outage_cursor
                            assert any(
                                entry.event.HasField("turn_completed")
                                and entry.event.turn_completed.turn_id == outage_command_id
                                for entry in outage_suffix
                            )
                            assert [entry.cursor for entry in [*prefix, *outage_suffix]] == list(
                                range(prefix[0].cursor, prefix[0].cursor + len(prefix) + len(outage_suffix))
                            )
                        finally:
                            history.process.process.send_signal(signal.SIGCONT)

                    await history.reader.redial()
                    await history.folded(UUID(session_id), outage_cursor)
                    recovered_page = await history.reader.read_session_events(session_id, limit=1000)
                    recovered = list(recovered_page.entries)
                    assert recovered == [*prefix, *outage_suffix]

                    next_command_id = str(uuid4())
                    await stub.SubmitInput(
                        harness_host_pb2.SubmitInputRequest(
                            session_id=session_id,
                            command_id=next_command_id,
                            text="Complete a fresh input after History recovery.",
                        ),
                        timeout=10,
                    )
                    next_request = await endpoint.next()
                    async with next_request as exchange:
                        await exchange.send(
                            *response_stream(
                                [Message("The fresh input completed after History recovery.")], model="test-model"
                            ).events
                        )
                    assert (
                        await turn_completed(stub, session_id, next_command_id, timeout_s=20)
                    ).status == event_pb2.TURN_STATUS_COMPLETED
                    recovered_snapshot = await store.get_session(session_id)
                    await history.folded(UUID(session_id), recovered_snapshot.cursor)
                    final_page = await history.reader.read_session_events(session_id, limit=1000)
                    final_history = list(final_page.entries)
                    final_sampler = await store.events(session_id, 0, limit=1000)
                    assert final_history == final_sampler
                    assert final_history[: len(prefix)] == prefix
                    assert [entry.cursor for entry in final_history] == list(
                        range(final_history[0].cursor, final_history[0].cursor + len(final_history))
                    )
                    shutdown = asyncio.create_task(running_runner.stop())
                    try:
                        await shutdown
                        # Orderly lease release fences the owned process domain and leaves the
                        # companion healthy in a fresh idle generation before Runner exits.
                        companion_status = await asyncio.to_thread(
                            inspect_codex_exec_server_container, codex_exec_server_container.socket
                        )
                        assert companion_status.generation != initial_companion.generation
                        assert not companion_status.owned
                        await asyncio.to_thread(codex_exec_server_container.check_health)
                    finally:
                        if not shutdown.done():
                            shutdown.cancel()
                            await asyncio.gather(shutdown, return_exceptions=True)
                    session = await store.get_session(session_id)
                    await history.folded(UUID(session_id), session.cursor)
                    original = await store.events(session_id, 0, limit=1000)
                    page = await history.reader.read_session_events(session_id, limit=1000)
                    assert list(page.entries) == original
                    setup_events = [entry.event for entry in page.entries]
                    assert sum(event.HasField("setup_started") for event in setup_events) == 1
                    assert [
                        event.setup_finished.exit_code for event in setup_events if event.HasField("setup_finished")
                    ] == [0]
                    assert (
                        b"".join(event.setup_output.stdout for event in setup_events if event.HasField("setup_output"))
                        == b"gateway setup stdout\n"
                    )
                    assert (
                        b"".join(event.setup_output.stderr for event in setup_events if event.HasField("setup_output"))
                        == b"gateway setup stderr\n"
                    )
                    answers = [
                        entry
                        for entry in page.entries
                        if entry.event.HasField("item_completed")
                        and entry.event.item_completed.text == "Patched artifact: after"
                    ]
                    assert len(answers) == 1
                    assert answers[0].event.source_sequences
                    await history.folded(UUID(session_id), session.cursor)
                    summary = await history.reader.get_session_summary(session_id)
                    assert summary.sandbox.uid == sandbox.uid


if __name__ == "__main__":
    pytest_bazel.main()

# Generated protobuf stubs require their direct typing dependency.
# gazelle:include_dep @pypi//protobuf
