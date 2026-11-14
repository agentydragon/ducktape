"""Real central sampling through Gateway, Runner, exec-server and authenticated History."""

import asyncio
import base64
import io
import json
import os
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
from PIL import Image

from agentplane.central_sampler.api import sampler_pb2, sampler_pb2_grpc
from agentplane.central_sampler.testing import Endpoint, create_request, go_sampler, settings
from agentplane.central_sampler.testing_store import Store
from agentplane.env import config as configuration
from agentplane.env.e2e.testing import launches
from agentplane.env.e2e.testing.gateway import serve_gateway
from agentplane.env.e2e.testing.process import RunningRunner
from agentplane.env.runtime.control import environment_runtime_pb2
from agentplane.env.runtime.execution import executor_pb2, executor_pb2_grpc
from agentplane.env.runtime.execution.testing.codex_exec_server_container import (
    CodexExecServerContainer,
    inspect_codex_exec_server_container,
)
from agentplane.grpc.calls import dial
from agentplane.history_service.testing.go_service import WRITER_AUDIENCE, go_history_service, migrate_history_database
from agentplane.native.codex.testing.responses_sse import CustomToolCall, Message, response_stream
from agentplane.net.peertls.config import MTLS
from agentplane.proto import event_pb2, session_pb2
from agentplane.runner_gateway.testing.gateway_process import run_gateway
from agentplane.sandboxes.testing.kubernetes import SandboxCluster, sandbox_cluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.electric_role import ensure_electric_role
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.postgres import create_temporary_database
from util.testing.sqlalchemy_engines import postgres_async_engine

# gazelle:include_dep //agentplane/env/runtime/execution/testing:codex_exec_server_container
pytest_plugins = ("agentplane.env.runtime.execution.testing.codex_exec_server_container",)


@asynccontextmanager
async def _history_tunnel(target: str) -> AsyncIterator[str]:
    """Forward CONNECT to the History test listener while preserving end-to-end TLS."""
    tasks: set[asyncio.Task[None]] = set()
    host, port = target.rsplit(":", 1)

    async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(10):
                headers = await reader.readuntil(b"\r\n\r\n")
            if headers.split(b"\r\n", 1)[0] != f"CONNECT {target} HTTP/1.1".encode():
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            remote, upstream = await asyncio.open_connection(host, int(port))
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            forward = asyncio.create_task(pump(reader, upstream))
            backward = asyncio.create_task(pump(remote, writer))
            try:
                await asyncio.wait((forward, backward), return_when=asyncio.FIRST_COMPLETED)
            finally:
                forward.cancel()
                backward.cancel()
                await asyncio.gather(forward, backward, return_exceptions=True)
        finally:
            writer.close()
            if upstream is not None:
                upstream.close()
                with suppress(ConnectionError):
                    await upstream.wait_closed()
            with suppress(ConnectionError):
                await writer.wait_closed()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(handle(reader, writer))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    address = server.sockets[0].getsockname()
    try:
        yield f"{address[0]}:{address[1]}"
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


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
    sampler_audience = "test-central-sampler"
    gateway_account = ServiceAccountRef(namespace=sandbox.namespace, name="gateway")
    gateway_audience = "test-central-gateway"
    gateway_token = tmp_path / "gateway-token"
    gateway_token.write_text(await api_server.workload_token(gateway_account, [gateway_audience]))
    sampler_token = tmp_path / "sampler-token"
    sampler_token.write_text(await api_server.workload_token(sampler, [gateway_audience]))
    sampler_api_token = tmp_path / "sampler-api-token"
    sampler_api_token.write_text(await api_server.workload_token(sampler, [sampler_audience]))
    wrong_audience_token = tmp_path / "wrong-audience-token"
    wrong_audience_token.write_text(await api_server.workload_token(sampler, ["test-central-wrong-audience"]))
    unauthorized_account = ServiceAccountRef(namespace=sampler.namespace, name="unauthorized")
    unauthorized_token = tmp_path / "unauthorized-account-token"
    unauthorized_token.write_text(await api_server.workload_token(unauthorized_account, [sampler_audience]))
    history_token = tmp_path / "history-token"
    history_token.write_text(await api_server.workload_token(sampler, [WRITER_AUDIENCE]))
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
                dispatcher_callers=(gateway_account,),
                gateway_callers=(gateway_account,),
                workload=gateway_account,
                executor_callers=(sampler,),
                dispatcher_audience="test-central-dispatcher",
                gateway_audience=gateway_audience,
                runner_proxy_token_file=gateway_token,
            ) as gateway,
            go_history_service(
                history_database,
                api=api_server,
                directory=tmp_path,
                writer_namespaces=[sampler.namespace, sandbox.namespace],
                sampler_accounts=[sampler],
                sampler_identity_callers=True,
                policy_namespace=sandbox.namespace,
                svid_ca=gateway.environment_runtime_ca,
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
                    tls=gateway.issue_environment_runtime_tls(
                        sandbox.environment_runtime_identity_uri, tmp_path / "environment-runtime-transport"
                    )
                ),
            )
            async with (
                _history_tunnel(history.ingress_target) as history_proxy,
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
                    controller_credentials=gateway.controller_credentials(tmp_path / "controller-transport"),
                    incarnation_uid=sandbox.pod_uid,
                ) as running_runner,
            ):
                await gateway.wait_for_runner(gateway.gateway_client(gateway_token), sandbox.destination)
                sampler_settings = settings(
                    database_url,
                    endpoint.origin,
                    None,
                    "/opt/codex/bin/codex",
                    code_mode_host,
                    undeclared_outputs_dir(),
                    gateway_target=gateway.target,
                    gateway_tls=gateway.client_tls,
                    gateway_token_file=sampler_token,
                    history_target=history.ingress_target,
                    history_token_file=history_token,
                    history_tls=history.history_tls,
                )
                request = create_request(tmp_path / "workspace")
                request.execution_identity.name = sandbox.execution_identity_name
                request.execution_identity.uid = sandbox.execution_identity_uid
                request.executor.CopyFrom(sandbox.destination)
                request.spec.placement.central_sampler_v1.executor_environment.name = sandbox.destination.target.name
                request.spec.placement.central_sampler_v1.executor_environment.uid = sandbox.destination.target.uid
                request.expected_incarnation_uid = sandbox.pod_uid
                request.setup_script = (
                    "printf before > artifact.txt\n"
                    "printf 'gateway setup stdout\\n'\n"
                    "printf 'gateway setup stderr\\n' >&2\n"
                )
                session_id = request.session_id
                sampler_settings = sampler_settings.model_copy(
                    update={"audience": sampler_audience, "allowed_accounts": {f"{sampler.namespace}/{sampler.name}"}}
                )
                sampler_metadata = (("authorization", f"Bearer {sampler_api_token.read_text().strip()}"),)
                async with (
                    go_sampler(sampler_settings, api_server.kubeconfig, undeclared_outputs_dir()) as sampler_target,
                    grpc.aio.insecure_channel(sampler_target) as channel,
                ):
                    stub = sampler_pb2_grpc.SamplerStub(channel)
                    for token, expected_code in (
                        (wrong_audience_token, grpc.StatusCode.UNAUTHENTICATED),
                        (unauthorized_token, grpc.StatusCode.PERMISSION_DENIED),
                    ):
                        with pytest.raises(grpc.aio.AioRpcError) as rejected:
                            await stub.CreateSession(
                                request,
                                metadata=(("authorization", f"Bearer {token.read_text().strip()}"),),
                                timeout=10,
                            )
                        assert rejected.value.code() == expected_code
                    assert not await store.events(session_id, 0, limit=1000)
                    with pytest.raises(grpc.aio.AioRpcError) as absent_session:
                        await stub.GetSession(
                            sampler_pb2.GetSessionRequest(session_id=session_id), metadata=sampler_metadata, timeout=10
                        )
                    assert absent_session.value.code() == grpc.StatusCode.NOT_FOUND
                    await stub.CreateSession(request, metadata=sampler_metadata, timeout=10)
                    await stub.SubmitInput(
                        environment_runtime_pb2.SubmitInputRequest(
                            session_id=session_id, command_id="shell-and-patch", text="Write, patch and read the file."
                        ),
                        metadata=sampler_metadata,
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
                    event_stream = stub.FollowEvents(
                        sampler_pb2.FollowEventsRequest(session_id=session_id, follow=True), metadata=sampler_metadata
                    )
                    try:
                        async with asyncio.timeout(20):
                            async for frame in event_stream:
                                if frame.entry.event.HasField("turn_completed"):
                                    assert frame.entry.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
                                    break
                            else:
                                pytest.fail("sampler stream ended before the turn completed")
                    finally:
                        event_stream.cancel()
                    async with dial(
                        gateway.target, channel_credentials=gateway.channel_credentials()
                    ) as gateway_channel:
                        stale_stream = executor_pb2_grpc.ExecutorStub(gateway_channel).OpenSession(
                            executor_pb2.OpenSessionRequest(
                                scope=executor_pb2.ExecutionScope(
                                    destination=sandbox.destination,
                                    expected_incarnation_uid="stale-" + sandbox.pod_uid,
                                    session_id=session_id,
                                ),
                                request_id=str(uuid4()),
                            ),
                            metadata=(("authorization", f"Bearer {sampler_token.read_text().strip()}"),),
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
                        environment_runtime_pb2.SubmitInputRequest(
                            session_id=session_id,
                            command_id=outage_command_id,
                            text="Complete this input while History is unavailable.",
                        ),
                        metadata=sampler_metadata,
                        timeout=10,
                    )
                    outage_request = await endpoint.next()

                    async def wait_for_turn_completion(after_cursor: int, turn_id: str) -> None:
                        event_stream = stub.FollowEvents(
                            sampler_pb2.FollowEventsRequest(
                                session_id=session_id, after_cursor=after_cursor, follow=True
                            ),
                            metadata=sampler_metadata,
                        )
                        try:
                            async with asyncio.timeout(20):
                                async for frame in event_stream:
                                    if frame.entry.event.HasField("turn_completed"):
                                        assert frame.entry.event.turn_completed.turn_id == turn_id
                                        assert (
                                            frame.entry.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
                                        )
                                        return
                                pytest.fail("sampler stream ended before the turn completed")
                        finally:
                            event_stream.cancel()

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
                            await wait_for_turn_completion(prefix[-1].cursor, outage_command_id)
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
                        environment_runtime_pb2.SubmitInputRequest(
                            session_id=session_id,
                            command_id=next_command_id,
                            text="Complete a fresh input after History recovery.",
                        ),
                        metadata=sampler_metadata,
                        timeout=10,
                    )
                    next_request = await endpoint.next()
                    async with next_request as exchange:
                        await exchange.send(
                            *response_stream(
                                [Message("The fresh input completed after History recovery.")], model="test-model"
                            ).events
                        )
                    await wait_for_turn_completion(outage_cursor, next_command_id)
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
