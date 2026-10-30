"""Real central sampling through Gateway, Runner, exec-server and authenticated History."""

import asyncio
import base64
import io
import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
from PIL import Image

from agentplane.grpc.calls import dial
from agentplane.history_service.testing.go_service import WRITER_AUDIENCE, go_history_service, migrate_history_database
from agentplane.native.codex.testing.responses_sse import CustomToolCall, Message, response_stream
from agentplane.proto import event_pb2, execution_pb2, runner_pb2
from agentplane.runner import configuration, runner_service_pb2
from agentplane.runner.testing import launches
from agentplane.runner.testing.codex_exec_server_container import CodexExecServerContainer
from agentplane.runner.testing.gateway import serve_gateway
from agentplane.runner.testing.process import RunningRunner
from agentplane.runner_gateway import executor_gateway_pb2_grpc
from agentplane.runner_gateway.testing.gateway_process import run_gateway
from agentplane.runner_transport.configuration import MTLS
from agentplane.sandboxes.testing.kubernetes import sandbox_cluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.electric_role import ensure_electric_role
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.x.central_sampler import sampler_pb2
from agentplane.x.central_sampler.settings import Settings
from agentplane.x.central_sampler.testing import METADATA, Endpoint, open_request, service, settings
from agentplane.x.central_sampler.testing_store import Store
from util.testing.postgres import create_temporary_database
from util.testing.sqlalchemy_engines import postgres_async_engine
from util.testing.undeclared_outputs import undeclared_outputs_dir


async def test_gateway_shell_patch_and_history_survive_executor_loss(
    database_url: str,
    postgres_admin_url: str,
    api_server: KubernetesApiServer,
    codex_exec_server_container: CodexExecServerContainer,
    code_mode_host: str,
    tmp_path: Path,
) -> None:
    sampler = ServiceAccountRef(namespace="test-central", name="sampler")
    gateway_account = ServiceAccountRef(namespace="test-central", name="gateway")
    gateway_audience = "test-central-gateway"
    gateway_token = tmp_path / "gateway-token"
    gateway_token.write_text(await api_server.workload_token(gateway_account, [gateway_audience]))
    sampler_token = tmp_path / "sampler-token"
    sampler_token.write_text(await api_server.workload_token(sampler, [gateway_audience]))
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
            sandbox_cluster(api_server) as sandbox,
            serve_gateway() as runner_gateway,
            run_gateway(
                api_server.kubeconfig,
                database_url=gateway_database,
                namespace=sandbox.namespace,
                dispatcher_callers=(gateway_account,),
                gateway_callers=(gateway_account,),
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
            ) as history,
        ):
            store = Store(engine)
            config = launches.config(
                runner_pb2.HARNESS_CODEX,
                endpoint.origin,
                state_dir=tmp_path / "runner-state",
                home=tmp_path / "runner-home",
                codex_executor_url=codex_exec_server_container.url,
                codex_executor_socket=codex_exec_server_container.socket,
            )
            config = replace(config, launch=replace(config.launch, sandbox=sandbox.identity))
            control = configuration.RunnerControl(
                listen_address="127.0.0.1:9090",
                security=MTLS(
                    mode="mtls",
                    tls=gateway.issue_runner_tls(sandbox.runner_identity_uri, tmp_path / "runner-transport"),
                ),
            )
            ingress = configuration.Endpoint(
                target=history.ingress_target, bearer=await history.writer_token(sandbox.owner)
            )
            async with RunningRunner(
                config,
                runner_gateway,
                history=history.reader,
                ingress_endpoint=ingress,
                control_endpoint=control,
                observe_control=False,
            ) as running_runner:
                await gateway.wait_for_runner(gateway.gateway_client(gateway_token), sandbox.destination)
                values = settings(
                    database_url,
                    endpoint.origin,
                    codex_exec_server_container.url,
                    "/opt/codex/bin/codex",
                    code_mode_host,
                    undeclared_outputs_dir(),
                ).model_dump()
                values.update(
                    direct_executor_target=None,
                    direct_executor_insecure_localtest=False,
                    gateway_target=gateway.target,
                    gateway_token_file=sampler_token,
                    history_target=history.ingress_target,
                    history_token_file=history_token,
                )
                sampler_settings = Settings.model_validate(values)
                request = open_request(tmp_path / "workspace")
                request.executor.CopyFrom(sandbox.destination)
                request.expected_incarnation_uid = sandbox.pod_uid
                request.setup_script = (
                    "printf before > artifact.txt\n"
                    "printf 'gateway setup stdout\\n'\n"
                    "printf 'gateway setup stderr\\n' >&2\n"
                )
                session_id = request.session_id
                async with service(sampler_settings) as stub:
                    await stub.OpenSession(request, metadata=METADATA, timeout=10)
                    await stub.SubmitInput(
                        runner_service_pb2.SubmitInputRequest(
                            session_id=session_id, command_id="shell-and-patch", text="Write, patch and read the file."
                        ),
                        metadata=METADATA,
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
                    stream = stub.FollowEvents(
                        sampler_pb2.FollowEventsRequest(session_id=session_id, follow=True), metadata=METADATA
                    )
                    try:
                        async with asyncio.timeout(20):
                            async for frame in stream:
                                if frame.entry.event.HasField("turn_completed"):
                                    assert frame.entry.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
                                    break
                            else:
                                pytest.fail("sampler stream ended before the turn completed")
                    finally:
                        stream.cancel()
                    async with dial(gateway.target) as channel:
                        stream = executor_gateway_pb2_grpc.ExecutorGatewayStub(channel).OpenSession(
                            execution_pb2.OpenSessionRequest(
                                scope=execution_pb2.ExecutionScope(
                                    destination=sandbox.destination,
                                    expected_incarnation_uid=sandbox.pod_uid,
                                    session_id=session_id,
                                ),
                                request_id=str(uuid4()),
                            ),
                            metadata=(("authorization", f"Bearer {sampler_token.read_text().strip()}"),),
                        )
                        with pytest.raises(grpc.aio.AioRpcError) as rejected:
                            await stream.read()
                        assert rejected.value.code() == grpc.StatusCode.FAILED_PRECONDITION
                    shutdown = asyncio.create_task(running_runner.stop())
                    try:
                        # Model kubelet restarting PID 1 after namespace cleanup. Runner waits for
                        # that fresh idle generation before releasing executor ownership.
                        companion = codex_exec_server_container.container.get_wrapped_container()
                        await asyncio.to_thread(companion.wait, timeout=10)
                        await asyncio.to_thread(companion.start)
                        await shutdown
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
