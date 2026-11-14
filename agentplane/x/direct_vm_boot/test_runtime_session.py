"""Drive a real Dispatcher Session through the packaged guest Claude harness."""

from __future__ import annotations

import asyncio
import os
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
from grpc_health.v1 import health_pb2, health_pb2_grpc

from agentplane.env.config import (
    Configuration,
    Endpoint,
    EnvironmentRuntimeControl,
    EnvironmentVariables,
    Harness,
    SVIDEndpoint,
    VMEnvironment,
)
from agentplane.env.e2e.testing import events, launches
from agentplane.env.e2e.testing.history_tunnel import history_tunnel
from agentplane.env.e2e.testing.model_config import model_config
from agentplane.grpc.calls import ServiceError
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.testing.go_service import GoHistoryService, go_history_service
from agentplane.native.claude.testing.messages import AnthropicMessages
from agentplane.net.peertls.config import EGRESS_PROXY_URI, HISTORY_SERVICE_URI, MTLS, NOTIFICATIONS_URI
from agentplane.proto import event_log_pb2, event_pb2, session_pb2
from agentplane.runner_gateway.dispatcher.api.session import session_dispatcher_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient, accepted_session, admitted
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.system_tests.vm.configuration import RESOURCES, SUPERVISION_KERNEL_ARG
from agentplane.system_tests.vm.guest_supervision_probe import VM_SUPERVISION_PROBE, assert_vm_supervision_probe
from agentplane.testing.claude_model import ClaudeModel
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.testing.scripted_model import ShellCall, Text
from agentplane.testing.tls import CertificateAuthority, issue_workload_leaf
from agentplane.x.direct_vm_boot.platform_services import (
    CLI_PLATFORM_PROBE,
    assert_credential_rotation_refuses_old_svid,
    assert_platform_cli_output,
    platform_services,
)
from agentplane.x.direct_vm_boot.runtime_guest import (
    authenticated_readiness,
    gateway_address,
    guest_image_bind_mounts,
    runtime_guest_container,
    sandbox_with_runtime_gateway,
    write_guest_inputs,
)
from agentplane.x.direct_vm_boot.session_helpers import (
    assert_guest_setup_is_supervised,
    history_turn_completed,
    wait_for_session_ready,
)
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep //agentplane/testing:native_gateway_fixtures
# gazelle:include_dep //agentplane/history_service/testing:fixtures
pytest_plugins = ("agentplane.testing.native_gateway_fixtures", "agentplane.history_service.testing.fixtures")


@pytest.fixture
async def sandbox(api_server: KubernetesApiServer) -> AsyncIterator[SandboxCluster]:
    async with sandbox_with_runtime_gateway(api_server) as started:
        yield started


@pytest.fixture
async def history_service(
    service_history_db_url: str,
    api_server: KubernetesApiServer,
    sandbox: SandboxCluster,
    gateway_service: GatewayProcess,
    tmp_path: Path,
) -> AsyncIterator[GoHistoryService]:
    async with go_history_service(
        service_history_db_url,
        api=api_server,
        directory=tmp_path,
        writer_namespaces=(sandbox.namespace,),
        svid_ca=gateway_service.environment_runtime_ca,
        policy_namespace=sandbox.namespace,
    ) as service:
        yield service


@pytest.fixture
async def ingress(
    history_service: GoHistoryService,
    control: EnvironmentRuntimeControl,
    gateway_service: GatewayProcess,
    tmp_path: Path,
) -> AsyncIterator[Endpoint]:
    wrong_history_control = os.environ.get("DIRECT_VM_SESSION_WRONG_HISTORY_SERVER_IDENTITY_CONTROL") == "1"
    if wrong_history_control:
        assert isinstance(control.security, MTLS)
        assert control.security.tls.cert_file is not None
        assert control.security.tls.key_file is not None
        async with (
            _wrong_history_tls_bridge(
                history_service=history_service,
                ca=gateway_service.environment_runtime_ca,
                runtime_client_cert=Path(control.security.tls.cert_file),
                runtime_client_key=Path(control.security.tls.key_file),
                directory=tmp_path / "wrong-history-tls-bridge",
            ) as target,
            history_tunnel(target, listen_host="0.0.0.0", advertised_host="10.0.2.1") as proxy,
        ):
            yield Endpoint(target=target, svid=SVIDEndpoint(server_uri=HISTORY_SERVICE_URI, proxy_address=proxy))
        return

    async with history_tunnel(
        history_service.ingress_target, listen_host="0.0.0.0", advertised_host="10.0.2.1"
    ) as proxy:
        yield Endpoint(
            target=history_service.ingress_target,
            svid=SVIDEndpoint(server_uri=HISTORY_SERVICE_URI, proxy_address=proxy),
        )


@asynccontextmanager
async def _wrong_history_tls_bridge(
    *,
    history_service: GoHistoryService,
    ca: CertificateAuthority,
    runtime_client_cert: Path,
    runtime_client_key: Path,
    directory: Path,
) -> AsyncIterator[str]:
    """Proxy TLS to the real History service while presenting only its URI identity incorrectly."""
    await asyncio.to_thread(directory.mkdir, parents=True)
    server_cert, server_key = await asyncio.to_thread(
        issue_workload_leaf, ca, EGRESS_PROXY_URI, directory, dns_name="localhost"
    )
    downstream_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    downstream_tls.load_cert_chain(server_cert, server_key)
    downstream_tls.load_verify_locations(cadata=ca.cert_pem.decode())
    downstream_tls.verify_mode = ssl.CERT_REQUIRED
    downstream_tls.set_alpn_protocols(["h2"])

    upstream_tls = ssl.create_default_context(cadata=ca.cert_pem.decode())
    upstream_tls.load_cert_chain(runtime_client_cert, runtime_client_key)
    upstream_tls.set_alpn_protocols(["h2"])
    history_dns_name, history_port = history_service.ingress_target.rsplit(":", 1)
    assert history_dns_name == "localhost", history_service.ingress_target
    log_path = undeclared_outputs_dir() / "wrong-history-tls-bridge.log"
    log_lock = asyncio.Lock()

    async def record(event: str) -> None:
        async with log_lock:
            await asyncio.to_thread(_append_bridge_log, log_path, event)

    async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()

    handlers: set[asyncio.Task[None]] = set()

    async def relay(reader: asyncio.StreamReader, downstream: asyncio.StreamWriter) -> None:
        upstream: asyncio.StreamWriter | None = None
        pumps: tuple[asyncio.Task[None], ...] = ()
        try:
            async with asyncio.timeout(5):
                upstream_reader, upstream = await asyncio.open_connection(
                    "127.0.0.1", int(history_port), ssl=upstream_tls, server_hostname=history_dns_name
                )
            await record("opened authenticated upstream TLS connection to History")
            pumps = (
                asyncio.create_task(pump(reader, upstream)),
                asyncio.create_task(pump(upstream_reader, downstream)),
            )
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await record(f"relay closed after {type(error).__name__}")
        finally:
            for task in pumps:
                if not task.done():
                    task.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            downstream.close()
            if upstream is not None:
                upstream.close()
            for writer in (downstream, upstream):
                if writer is not None:
                    try:
                        async with asyncio.timeout(2):
                            await writer.wait_closed()
                    except TimeoutError:
                        writer.transport.abort()
                        await record("TLS relay writer shutdown exceeded its 2s cleanup bound")
                    except (ConnectionError, OSError) as error:
                        await record(f"TLS relay writer shutdown raised {type(error).__name__}")

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(relay(reader, writer))
        handlers.add(task)
        task.add_done_callback(handlers.discard)

    server = await asyncio.start_server(accept, "127.0.0.1", 0, ssl=downstream_tls, ssl_handshake_timeout=5)
    assert server.sockets is not None
    port = server.sockets[0].getsockname()[1]
    target = f"127.0.0.1:{port}"
    await record("TLS bridge listener started")
    try:
        # This real, read-only health RPC proves DNS+CA verification, downstream Runtime mTLS, and
        # the bridge's upstream Runtime mTLS to the actual healthy History process before QEMU starts.
        credentials = grpc.ssl_channel_credentials(
            root_certificates=ca.cert_pem,
            private_key=await asyncio.to_thread(runtime_client_key.read_bytes),
            certificate_chain=await asyncio.to_thread(runtime_client_cert.read_bytes),
        )
        channel = grpc.aio.secure_channel(
            target,
            credentials,
            options=(("grpc.default_authority", "localhost"), ("grpc.ssl_target_name_override", "localhost")),
        )
        async with asyncio.timeout(10), channel:
            response = await health_pb2_grpc.HealthStub(channel).Check(health_pb2.HealthCheckRequest(), timeout=5)
        assert response.status == health_pb2.HealthCheckResponse.SERVING, response
        await record("preflight real History Health.Check returned SERVING through both mTLS legs")
        yield target
    finally:
        server.close()
        await server.wait_closed()
        pending = list(handlers)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await record("TLS bridge listener and relay tasks stopped")


def _append_bridge_log(path: Path, event: str) -> None:
    with path.open("a", encoding="utf-8") as log:
        log.write(f"{event}\n")


@pytest.fixture
async def endpoint() -> AsyncIterator[AnthropicMessages]:
    server = AnthropicMessages(bind_host="0.0.0.0")
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
def model(endpoint: AnthropicMessages) -> ClaudeModel:
    return ClaudeModel(endpoint)


async def assert_guest_systemd_supervision_passed(state: Path, outputs: Path, docker_done: asyncio.Task[int]) -> None:
    expected_receipt = '{"real_systemd_supervision":"passed"}\n'
    expected_marker = f"AGENTPLANE_GUEST_SUPERVISION_PASSED {expected_receipt.rstrip()}"
    # The start wrapper emits serial records before its private checks finish.
    # The existing QEMU lifetime bounds this wait, including a wedged fixture.
    while True:
        serial = await asyncio.to_thread((outputs / "runtime.serial.log").read_text, errors="replace")
        assert "AGENTPLANE_GUEST_SUPERVISION_FAILED" not in serial, "guest systemd supervision failed"
        if expected_marker in serial.splitlines():
            break
        assert not docker_done.done(), "QEMU guest exited before the supervision pass receipt"
        await asyncio.sleep(0.05)
    result = (state / "testing/supervision/result.json").read_text()
    assert result == expected_receipt, "guest supervision result did not match the exact pass receipt"
    assert serial.count("--- PASS: TestSystemdKernelFixture ") == 1, "guest serial lacks the parent Go test pass"


async def _run_shell_turn(
    *,
    dispatcher: SessionDispatcherClient,
    history: HistoryServiceClient,
    model: ClaudeModel,
    sandbox: SandboxCluster,
    session_id: str,
    input_text: str,
    command_id: str,
    call_id: str,
    shell_command: str,
    expected_output: str,
    final_text: str,
) -> tuple[list[event_log_pb2.EventEntry], str]:
    dispatch = await dispatcher.submit_input(session_id, command_id, sandbox.name, sandbox.uid, input_text)
    receipt = (await admitted(dispatcher, dispatch)).admission

    request = await model.request()
    assert request.user_texts[-1] == input_text
    await model.reply(request, ShellCall(call_id, shell_command))

    tool_request = await model.request()
    (output,) = [output for output in tool_request.tool_outputs if output.call_id == call_id]
    assert output.contains(expected_output)
    (native_output,) = [result for result in tool_request.native.tool_results if result.tool_use_id == call_id]
    assert not native_output.is_error
    assert native_output.text.splitlines()[0] == expected_output
    await model.reply(tool_request, Text(final_text))
    entries = await history_turn_completed(history, session_id, receipt.cursor - 1)
    return entries, native_output.text


async def test_packaged_claude_session_runs_shell_and_accepts_next_input(
    tmp_path: Path,
    postgres_admin_url: str,
    api_server: KubernetesApiServer,
    sandbox: SandboxCluster,
    control: EnvironmentRuntimeControl,
    gateway_service: GatewayProcess,
    client_token: Path,
    dispatcher: SessionDispatcherClient,
    history_service: GoHistoryService,
    ingress: Endpoint,
    endpoint: AnthropicMessages,
    model: ClaudeModel,
) -> None:
    inputs = tmp_path / "inputs"
    state = tmp_path / "state"
    outputs = tmp_path / "outputs"
    inputs.mkdir()
    state.mkdir()
    outputs.mkdir()
    guest_image_mounts = guest_image_bind_mounts(inputs)

    with pytest.raises(ServiceError) as health_reader:
        await history_service.reader.get_session_summary(str(uuid4()))
    assert health_reader.value.code == grpc.StatusCode.NOT_FOUND
    await history_service.process.health(serving=True)

    hostfwd_address = gateway_address(sandbox.uid)
    container_name = f"agentplane-runtime-session-{UUID(sandbox.uid).hex[:16]}"
    requested_command_ids = (str(uuid4()), str(uuid4()))
    assert len(set(requested_command_ids)) == 2
    resources = RESOURCES.model_copy(update={"executor": None})
    if os.environ.get("DIRECT_VM_SETUP_WRONG_PROCESS_LIMIT_CONTROL") == "1":
        resources = resources.model_copy(update={"process": RESOURCES.process.model_copy(update={"pids_max": 96})})
    async with (
        platform_services(
            postgres_admin_url=postgres_admin_url,
            sandbox=sandbox,
            ca=gateway_service.environment_runtime_ca,
            directory=tmp_path / "platform",
            notifications_server_uri=(
                EGRESS_PROXY_URI
                if os.environ.get("DIRECT_VM_NOTIFICATION_WRONG_SERVER_IDENTITY_CONTROL") == "1"
                else NOTIFICATIONS_URI
            ),
        ) as platform,
        model_config(listen_host="0.0.0.0", advertised_host="10.0.2.1") as model_config_target,
    ):
        config = Configuration(
            version=2,
            state_dir="/state",
            workspace_root="/state/workspaces",
            initialization_cwd="/state/workspaces",
            control=EnvironmentRuntimeControl(listen_address="0.0.0.0:9090", security=control.security),
            ingress=ingress,
            model_config_target=model_config_target,
            platform_instructions="Exercise the packaged Claude harness and retain the workspace marker.",
            harness_environment=EnvironmentVariables(
                values={
                    "ANTHROPIC_AUTH_TOKEN": launches.TOKEN,
                    "AGENTPLANE_EGRESS_GRPC_URL": platform.guest_url,
                    "AGENTPLANE_NOTIFICATIONS_GRPC_URL": platform.guest_url,
                    "AGENTPLANE_EGRESS_GRPC_CREDENTIAL": "deliberately-forged-caller-token",
                    "AGENTPLANE_NOTIFICATIONS_GRPC_CREDENTIAL": "deliberately-forged-caller-token",
                    "HOME": "/state/home",
                    "NO_PROXY": "10.0.2.1,127.0.0.1,localhost",
                    "PATH": "/run/current-system/sw/bin:/run/agentplane/environment/bin",
                }
            ),
            claude=Harness(
                binary="/run/current-system/sw/bin/claude",
                base_url=f"http://10.0.2.1:{urlsplit(endpoint.origin).port}",
                token_env="ANTHROPIC_AUTH_TOKEN",
            ),
            environment=VMEnvironment(
                resources=resources, agent_uid=107, agent_gid=107, block_devices=("/dev/disk/by-id/virtio-workspace",)
            ),
        )
        write_guest_inputs(inputs, sandbox, config)
        async with runtime_guest_container(
            container_name=container_name,
            inputs=inputs,
            guest_image_mounts=guest_image_mounts,
            state=state,
            outputs=outputs,
            gateway_address=hostfwd_address,
            kernel_args=(SUPERVISION_KERNEL_ARG,),
        ) as docker_done:
            await assert_guest_systemd_supervision_passed(state, outputs, docker_done)
            ready_task = asyncio.create_task(
                authenticated_readiness(gateway_service, client_token, api_server, sandbox)
            )
            try:
                done, _ = await asyncio.wait((docker_done, ready_task), return_when=asyncio.FIRST_COMPLETED)
                assert docker_done not in done, "QEMU guest exited before authenticated Runtime readiness"
                initial_status = await ready_task
            finally:
                if not ready_task.done():
                    ready_task.cancel()
                    await asyncio.gather(ready_task, return_exceptions=True)

            session_id = uuid4()
            workspace = Path("/state/workspaces") / session_id.hex
            spec = launches.spec(session_pb2.HARNESS_CLAUDE, workspace, sandbox.identity)
            await dispatcher.unary(
                dispatcher.stub.CreateSession,
                session_dispatcher_pb2.CreateSessionRequest(session_id=str(session_id), spec=spec),
            )
            await accepted_session(dispatcher, session_id)

            marker = f"direct-vm-{session_id.hex}"
            marker_path = str(workspace / "session-marker.txt")
            first_command = (
                f"set -eu\nprintf '%s\\n' '{marker}' > '{marker_path}' && cat '{marker_path}'\n{VM_SUPERVISION_PROBE}"
            )
            first_entries, first_output = await _run_shell_turn(
                dispatcher=dispatcher,
                history=history_service.reader,
                model=model,
                sandbox=sandbox,
                session_id=str(session_id),
                input_text="Create the marker file and report the marker.",
                command_id=requested_command_ids[0],
                call_id=f"write-{session_id.hex}",
                shell_command=first_command,
                expected_output=marker,
                final_text="MARKER_WRITTEN",
            )
            assert_vm_supervision_probe(first_output, slice_name="agentplane-workloads.slice")
            second_entries, platform_output = await _run_shell_turn(
                dispatcher=dispatcher,
                history=history_service.reader,
                model=model,
                sandbox=sandbox,
                session_id=str(session_id),
                input_text="Read the existing marker file and report its contents.",
                command_id=requested_command_ids[1],
                call_id=f"read-{session_id.hex}",
                shell_command=f"set -eu\ncat '{marker_path}'\n{CLI_PLATFORM_PROBE}",
                expected_output=marker,
                final_text="MARKER_READ",
            )
            assert first_entries
            assert second_entries
            assert_platform_cli_output(platform_output, platform, sandbox)
            await assert_guest_setup_is_supervised(dispatcher, history_service.reader, spec)
            assert not docker_done.done(), "QEMU guest exited before both Session turns completed"
            final_status = await wait_for_session_ready(gateway_service, client_token, sandbox, str(session_id))
            assert final_status.environment_runtime_boot_id == initial_status.environment_runtime_boot_id
        await assert_credential_rotation_refuses_old_svid(platform, sandbox)

    serial = (outputs / "runtime.serial.log").read_text(errors="replace")
    runtime_records = [line for line in serial.splitlines() if '"logger":"environment-runtime-lifecycle"' in line]
    assert runtime_records, "packaged Runtime did not emit lifecycle records on the guest serial console"
    history_entries = list(
        (await history_service.reader.read_session_events(str(session_id), after_cursor=0, limit=1000)).entries
    )
    events.assert_contiguous(history_entries)
    events.assert_sourced(history_entries)
    command_ids = [
        entry.event.command_admitted.command.command_id for entry in events.of_kind(history_entries, "command_admitted")
    ]
    expected_command_ids = list(requested_command_ids)
    assert len(command_ids) == 2
    assert len(events.of_kind(history_entries, "turn_completed")) == 2
    assert [entry.event.turn_completed.status for entry in events.of_kind(history_entries, "turn_completed")] == [
        event_pb2.TURN_STATUS_COMPLETED,
        event_pb2.TURN_STATUS_COMPLETED,
    ]
    assert command_ids == expected_command_ids


if __name__ == "__main__":
    pytest_bazel.main()
