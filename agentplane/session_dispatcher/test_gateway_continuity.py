"""Active process and Runtime continuity through independent Environments proxy peers."""

import asyncio
import shlex
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import uuid4

import grpc
import pytest
import pytest_bazel
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.env.e2e.testing.config import RunnerTestConfig
from agentplane.env.e2e.testing.gateway import Gateway
from agentplane.env.e2e.testing.process import RunningRunner
from agentplane.env.e2e.testing.runtime_with_history import run_runtime_with_history
from agentplane.env.runtime.execution import executor_pb2, executor_pb2_grpc
from agentplane.env.runtime.execution.testing.executor_companion import ExecutorCompanion
from agentplane.environments.testing.process import EnvironmentServiceProcess, run_environment_service
from agentplane.grpc.calls import ServiceError
from agentplane.history_service.testing.go_service import WRITER, GoHistoryService, go_history_service
from agentplane.net.peertls.config import APP_URI, CENTRAL_SAMPLER_URI, GATEWAY_RUNTIME_PROXY_URI
from agentplane.proto import session_pb2
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.session_dispatcher.client import SandboxControlClient
from agentplane.session_dispatcher.dispatcher.api.session import session_dispatcher_pb2, session_dispatcher_pb2_grpc
from agentplane.session_dispatcher.testing.gateway_process import GatewayProcess, run_gateway
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.testing.tls import RUNNER_GATEWAY_TEST_DNS

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep //agentplane/env/e2e/testing:fixtures
# gazelle:include_dep //agentplane/history_service/testing:fixtures
# gazelle:include_dep //agentplane/testing:native_gateway_fixtures
pytest_plugins = (
    "agentplane.testing.native_gateway_fixtures",
    "agentplane.env.e2e.testing.fixtures",
    "agentplane.history_service.testing.fixtures",
    "agentplane.env.runtime.execution.testing.executor_companion",
)


@pytest.fixture
async def history_service(
    service_history_db_url: str,
    api_server: KubernetesApiServer,
    sandbox: SandboxCluster,
    runner_gateway: Gateway,
    tmp_path: Path,
) -> AsyncIterator[GoHistoryService]:
    async with go_history_service(
        service_history_db_url,
        api=api_server,
        directory=tmp_path,
        writer_namespaces=(sandbox.namespace, WRITER.namespace),
        svid_ca=runner_gateway.ca,
        policy_namespace=runner_gateway.sandbox.namespace,
    ) as service:
        yield service


@pytest.fixture
def sandbox_control(gateway_service: GatewayProcess) -> SandboxControlClient:
    return gateway_service.environment_runtime_client()


@pytest.fixture
async def runner(
    config: RunnerTestConfig,
    runner_gateway: Gateway,
    history_service: GoHistoryService,
    sandbox: SandboxCluster,
    gateway_service: GatewayProcess,
    sandbox_control: SandboxControlClient,
    api_server: KubernetesApiServer,
    tmp_path: Path,
) -> AsyncIterator[RunningRunner]:
    async with run_runtime_with_history(
        config,
        runner_gateway,
        history_service,
        sandbox,
        gateway_service.environment_service,
        sandbox_control,
        api=api_server,
        directory=tmp_path,
    ) as started:
        yield started


@pytest.mark.parametrize("harness", [session_pb2.HARNESS_CODEX], ids=["codex"])
class TestGatewayContinuity:
    @pytest.fixture
    def config(self, config: RunnerTestConfig, executor_companion: ExecutorCompanion) -> RunnerTestConfig:
        return replace(config, executor_url=executor_companion.url, executor_socket=executor_companion.socket)

    @pytest.fixture
    async def gateway_service(
        self, api_server: KubernetesApiServer, sandbox: SandboxCluster, dispatcher_database_url: str, tmp_path: Path
    ) -> AsyncIterator[GatewayProcess]:
        async with run_gateway(
            api_server.kubeconfig,
            database_url=dispatcher_database_url,
            namespace=sandbox.namespace,
            runtime_proxy_executor_callers=(CENTRAL_SAMPLER_URI,),
        ) as gateway:
            yield gateway

    @pytest.fixture
    async def sibling_environment_service(
        self,
        api_server: KubernetesApiServer,
        sandbox: SandboxCluster,
        gateway_service: GatewayProcess,
        runner: RunningRunner,
    ) -> AsyncIterator[EnvironmentServiceProcess]:
        """A second production Environments peer with an independent Runtime inventory/watch."""
        assert runner.process.exit_code is None
        async with run_environment_service(
            api_server.kubeconfig,
            namespace=sandbox.namespace,
            caller_uri=APP_URI,
            environment_runtime_ca=gateway_service.environment_service.ca,
            runtime_proxy_executor_callers=(CENTRAL_SAMPLER_URI,),
        ) as replica:
            replica_client = SandboxControlClient(
                control_target=replica.target,
                request_timeout_s=20,
                channel_credentials=replica.channel_credentials(APP_URI),
            )
            await replica.wait_for_runner(replica_client, sandbox.destination)
            yield replica

    async def test_two_environments_peers_keep_one_runtime_binding_and_active_execution(
        self,
        gateway_service: GatewayProcess,
        sibling_environment_service: EnvironmentServiceProcess,
        sandbox: SandboxCluster,
        runner: RunningRunner,
        tmp_path: Path,
    ) -> None:
        """Independent Environments peers can call one Runtime while either service restarts."""
        assert runner.process.exit_code is None
        gateway_a_client = gateway_service.environment_runtime_client()
        gateway_b_client = SandboxControlClient(
            control_target=sibling_environment_service.target,
            request_timeout_s=20,
            channel_credentials=sibling_environment_service.channel_credentials(APP_URI),
        )
        gateway_a_runner = gateway_a_client.runner(sandbox.destination)
        gateway_b_runner = gateway_b_client.runner(sandbox.destination)
        initial_a = await gateway_a_runner.status()
        initial_b = await gateway_b_runner.status()
        assert initial_a.environment_runtime_boot_id
        assert initial_b.environment_runtime_boot_id == initial_a.environment_runtime_boot_id
        await gateway_a_runner.list_sessions(expected_incarnation_uid=sandbox.pod_uid)
        await gateway_b_runner.list_sessions(expected_incarnation_uid=sandbox.pod_uid)

        scope = executor_pb2.ExecutionScope(
            destination=sandbox.destination,
            expected_incarnation_uid=sandbox.pod_uid,
            session_id=f"gateway-replica-{uuid4().hex}",
        )
        metadata: tuple[tuple[str, str], ...] = ()
        workspace = tmp_path
        ready = workspace / "replica-process-ready"
        release = workspace / "replica-process-release"
        pid_marker = workspace / "replica-process-pids"
        prefix = f"replica-prefix-{uuid4().hex}"
        suffix = f"replica-suffix-{uuid4().hex}"
        process_id = uuid4().hex
        shell = (
            f"printf '%s\\n' \"$$\" >> {shlex.quote(str(pid_marker))}; "
            f"printf '%s' {shlex.quote(prefix)}; "
            f": > {shlex.quote(str(ready))}; "
            f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.02; done; "
            f"printf '%s' {shlex.quote(suffix)}"
        )

        async def read_process(
            client: executor_pb2_grpc.ExecutorStub,
            execution_session_id: str,
            *,
            after_seq: int | None,
            wait_ms: int = 100,
        ) -> executor_pb2.ReadProcessResponse:
            request = executor_pb2.ReadProcessRequest(
                scope=scope,
                execution_session_id=execution_session_id,
                request_id=uuid4().hex,
                process_id=process_id,
                max_bytes=1024,
                wait_ms=wait_ms,
            )
            if after_seq is not None:
                request.after_seq = after_seq
            return cast(
                executor_pb2.ReadProcessResponse, await client.ReadProcess(request, metadata=metadata, timeout=5)
            )

        async with (
            grpc.aio.secure_channel(
                gateway_service.environment_service.target,
                gateway_service.environment_service.channel_credentials(CENTRAL_SAMPLER_URI),
            ) as original_channel,
            grpc.aio.secure_channel(
                sibling_environment_service.target, sibling_environment_service.channel_credentials(CENTRAL_SAMPLER_URI)
            ) as sibling_channel,
            grpc.aio.secure_channel(
                gateway_service.target, gateway_service.service_credentials(APP_URI)
            ) as dispatcher_channel,
        ):
            await asyncio.wait_for(original_channel.channel_ready(), timeout=10)
            await asyncio.wait_for(sibling_channel.channel_ready(), timeout=10)
            await asyncio.wait_for(dispatcher_channel.channel_ready(), timeout=10)
            executor_a = executor_pb2_grpc.ExecutorStub(original_channel)
            executor_b = executor_pb2_grpc.ExecutorStub(sibling_channel)
            dispatcher = session_dispatcher_pb2_grpc.SessionDispatcherStub(dispatcher_channel)
            opened = cast(
                grpc.aio.UnaryStreamCall[executor_pb2.OpenSessionRequest, executor_pb2.OpenSessionResponse],
                executor_a.OpenSession(
                    executor_pb2.OpenSessionRequest(scope=scope, request_id=uuid4().hex), metadata=metadata
                ),
            )
            opened_response = cast(executor_pb2.OpenSessionResponse, await opened.read())
            assert opened_response.status.outcome == executor_pb2.ExecutionStatus.OUTCOME_COMPLETED
            execution_session_id = opened_response.execution_session_id
            assert execution_session_id
            started = cast(
                executor_pb2.StartProcessResponse,
                await executor_a.StartProcess(
                    executor_pb2.StartProcessRequest(
                        scope=scope,
                        execution_session_id=execution_session_id,
                        request_id=uuid4().hex,
                        process_id=process_id,
                        argv=["/bin/sh", "-c", shell],
                        cwd=workspace.as_uri(),
                    ),
                    metadata=metadata,
                    timeout=10,
                ),
            )
            assert started.status.outcome == executor_pb2.ExecutionStatus.OUTCOME_COMPLETED
            assert started.process_id == process_id
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(15),
                wait=wait_fixed(0.02),
                retry=retry_if_exception_type(AssertionError),
                reraise=True,
            ):
                with attempt:
                    assert ready.exists()
            prefix_output = bytearray()
            cursor: int | None = None
            async with asyncio.timeout(15):
                while bytes(prefix_output) != prefix.encode():
                    response = await read_process(executor_a, execution_session_id, after_seq=cursor, wait_ms=500)
                    assert response.status.outcome == executor_pb2.ExecutionStatus.OUTCOME_COMPLETED
                    prefix_output.extend(b"".join(chunk.data for chunk in response.chunks))
                    cursor = response.next_seq - 1 if response.next_seq else None
            assert bytes(prefix_output) == prefix.encode()
            assert cursor is not None
            prefix_cursor = cursor
            process_identity = pid_marker.read_text().splitlines()
            assert len(process_identity) == 1

            # Real Runtime auth rejects stale values before Executor.StartProcess can create a process.
            runtime_client_tls = sibling_environment_service.issue_workload_tls(
                GATEWAY_RUNTIME_PROXY_URI, tmp_path / "direct-runtime-proxy-client"
            )
            assert runtime_client_tls.cert_file is not None
            assert runtime_client_tls.key_file is not None
            assert runtime_client_tls.ca_file is not None
            root_certificates, private_key, certificate_chain = await asyncio.gather(
                asyncio.to_thread(Path(runtime_client_tls.ca_file).read_bytes),
                asyncio.to_thread(Path(runtime_client_tls.key_file).read_bytes),
                asyncio.to_thread(Path(runtime_client_tls.cert_file).read_bytes),
            )
            runtime_credentials = grpc.ssl_channel_credentials(
                root_certificates=root_certificates, private_key=private_key, certificate_chain=certificate_chain
            )
            async with grpc.aio.secure_channel(
                runner.control_endpoint.listen_address,
                runtime_credentials,
                options=(("grpc.ssl_target_name_override", RUNNER_GATEWAY_TEST_DNS),),
            ) as runtime_channel:
                await asyncio.wait_for(runtime_channel.channel_ready(), timeout=10)
                direct_executor = executor_pb2_grpc.ExecutorStub(runtime_channel)
                current_runtime_metadata = {
                    "agentplane-sandbox-uid": sandbox.uid,
                    "agentplane-incarnation-uid": sandbox.pod_uid,
                    "agentplane-environment-runtime-boot-id": initial_a.environment_runtime_boot_id,
                }
                stale_runtime_cases = (
                    ("sandbox UID", "agentplane-sandbox-uid", "stale-sandbox-uid", grpc.StatusCode.PERMISSION_DENIED),
                    ("incarnation UID", "agentplane-incarnation-uid", "stale-incarnation", grpc.StatusCode.UNAVAILABLE),
                    (
                        "Runtime boot ID",
                        "agentplane-environment-runtime-boot-id",
                        "stale-runtime-boot",
                        grpc.StatusCode.UNAVAILABLE,
                    ),
                )
                for label, key, value, code in stale_runtime_cases:
                    stale_metadata = dict(current_runtime_metadata)
                    stale_metadata[key] = value
                    stale_marker = workspace / f"stale-{key}.executed"
                    stale_request = executor_pb2.StartProcessRequest(
                        scope=scope,
                        execution_session_id=execution_session_id,
                        request_id=uuid4().hex,
                        process_id=uuid4().hex,
                        argv=["/bin/sh", "-c", f"touch {shlex.quote(str(stale_marker))}"],
                        cwd=workspace.as_uri(),
                    )
                    with pytest.raises(grpc.aio.AioRpcError) as rejected:
                        await direct_executor.StartProcess(
                            stale_request, metadata=tuple(stale_metadata.items()), timeout=5
                        )
                    assert rejected.value.code() == code, label
                    assert not stale_marker.exists(), f"stale {label} started a process"

            async def check_environment_a_gap() -> None:
                assert gateway_service.environment_service.process.returncode is not None
                short_a_client = gateway_service.environment_runtime_client(request_timeout_s=0.25)
                with pytest.raises((ServiceError, TimeoutError)):
                    await short_a_client.list_sessions(sandbox.destination)
                during_gap = await gateway_b_runner.status()
                assert during_gap.environment_runtime_boot_id == initial_a.environment_runtime_boot_id
                await gateway_b_runner.list_sessions(expected_incarnation_uid=sandbox.pod_uid)
                resumed = cast(
                    grpc.aio.UnaryStreamCall[executor_pb2.OpenSessionRequest, executor_pb2.OpenSessionResponse],
                    executor_b.OpenSession(
                        executor_pb2.OpenSessionRequest(
                            scope=scope, request_id=uuid4().hex, resume_session_id=execution_session_id
                        ),
                        metadata=metadata,
                    ),
                )
                response = cast(executor_pb2.OpenSessionResponse, await resumed.read())
                assert response.status.outcome == executor_pb2.ExecutionStatus.OUTCOME_COMPLETED
                assert response.execution_session_id == execution_session_id
                gap_read = await read_process(executor_b, execution_session_id, after_seq=cursor, wait_ms=100)
                assert not gap_read.exited
                assert not gap_read.chunks
                resumed.cancel()

            await gateway_service.environment_service.restart(while_stopped=check_environment_a_gap)
            opened.cancel()
            await gateway_service.environment_service.wait_for_runner(gateway_a_client, sandbox.destination)
            recovered_a = await gateway_a_runner.status()
            recovered_b = await gateway_b_runner.status()
            assert recovered_a.environment_runtime_boot_id == initial_a.environment_runtime_boot_id
            assert recovered_b.environment_runtime_boot_id == initial_a.environment_runtime_boot_id
            await gateway_a_runner.list_sessions(expected_incarnation_uid=sandbox.pod_uid)
            await gateway_b_runner.list_sessions(expected_incarnation_uid=sandbox.pod_uid)

            resumed = cast(
                grpc.aio.UnaryStreamCall[executor_pb2.OpenSessionRequest, executor_pb2.OpenSessionResponse],
                executor_a.OpenSession(
                    executor_pb2.OpenSessionRequest(
                        scope=scope, request_id=uuid4().hex, resume_session_id=execution_session_id
                    ),
                    metadata=metadata,
                ),
            )
            resumed_response = cast(executor_pb2.OpenSessionResponse, await resumed.read())
            assert resumed_response.status.outcome == executor_pb2.ExecutionStatus.OUTCOME_COMPLETED
            assert resumed_response.execution_session_id == execution_session_id

            async def check_dispatcher_gap() -> None:
                assert gateway_service.process.returncode is not None
                with pytest.raises(grpc.aio.AioRpcError) as dispatcher_unavailable:
                    await dispatcher.ListSessions(session_dispatcher_pb2.ListSessionsRequest(), timeout=0.25)
                assert dispatcher_unavailable.value.code() in {
                    grpc.StatusCode.UNAVAILABLE,
                    grpc.StatusCode.DEADLINE_EXCEEDED,
                }
                during_gap_a = await gateway_a_runner.status()
                during_gap_b = await gateway_b_runner.status()
                assert during_gap_a.environment_runtime_boot_id == initial_a.environment_runtime_boot_id
                assert during_gap_b.environment_runtime_boot_id == initial_a.environment_runtime_boot_id
                await gateway_a_runner.list_sessions(expected_incarnation_uid=sandbox.pod_uid)
                await gateway_b_runner.list_sessions(expected_incarnation_uid=sandbox.pod_uid)
                gap_read = await read_process(executor_a, execution_session_id, after_seq=cursor, wait_ms=100)
                assert not gap_read.exited
                assert not gap_read.chunks

            old_dispatcher_process = gateway_service.process
            await gateway_service.restart(while_stopped=check_dispatcher_gap)
            assert old_dispatcher_process.returncode is not None
            await dispatcher.ListSessions(session_dispatcher_pb2.ListSessionsRequest(), timeout=5)
            assert not release.exists()
            release.touch()
            suffix_output = bytearray()
            post_recovery_reads: list[executor_pb2.ReadProcessResponse] = []
            async with asyncio.timeout(15):
                while True:
                    response = await read_process(executor_a, execution_session_id, after_seq=cursor, wait_ms=500)
                    post_recovery_reads.append(response)
                    assert response.status.outcome == executor_pb2.ExecutionStatus.OUTCOME_COMPLETED
                    suffix_output.extend(b"".join(chunk.data for chunk in response.chunks))
                    cursor = response.next_seq - 1 if response.next_seq else cursor
                    # Exit can precede the last output frame; drain the closed stream too.
                    if response.exited and response.closed and not response.chunks:
                        assert response.exit_code == 0
                        break
            assert bytes(suffix_output) == suffix.encode()
            assert cursor is not None
            assert cursor > prefix_cursor
            assert all(chunk.data != prefix.encode() for response in post_recovery_reads for chunk in response.chunks)
            assert pid_marker.read_text().splitlines() == process_identity

            fresh_process_id = uuid4().hex
            fresh_marker = workspace / "fresh-post-recovery-command"
            fresh_start = cast(
                executor_pb2.StartProcessResponse,
                await executor_a.StartProcess(
                    executor_pb2.StartProcessRequest(
                        scope=scope,
                        execution_session_id=execution_session_id,
                        request_id=uuid4().hex,
                        process_id=fresh_process_id,
                        argv=[
                            "/bin/sh",
                            "-c",
                            f"printf fresh; printf '%s\\n' \"$$\" >> {shlex.quote(str(fresh_marker))}",
                        ],
                        cwd=workspace.as_uri(),
                    ),
                    metadata=metadata,
                    timeout=10,
                ),
            )
            assert fresh_start.status.outcome == executor_pb2.ExecutionStatus.OUTCOME_COMPLETED
            assert fresh_start.process_id == fresh_process_id
            fresh_output = bytearray()
            fresh_cursor: int | None = None
            async with asyncio.timeout(15):
                while True:
                    request = executor_pb2.ReadProcessRequest(
                        scope=scope,
                        execution_session_id=execution_session_id,
                        request_id=uuid4().hex,
                        process_id=fresh_process_id,
                        max_bytes=1024,
                        wait_ms=500,
                    )
                    if fresh_cursor is not None:
                        request.after_seq = fresh_cursor
                    response = cast(
                        executor_pb2.ReadProcessResponse,
                        await executor_a.ReadProcess(request, metadata=metadata, timeout=5),
                    )
                    assert response.status.outcome == executor_pb2.ExecutionStatus.OUTCOME_COMPLETED
                    fresh_output.extend(b"".join(chunk.data for chunk in response.chunks))
                    fresh_cursor = response.next_seq - 1 if response.next_seq else fresh_cursor
                    if response.exited and response.closed and not response.chunks:
                        assert response.exit_code == 0
                        break
            assert bytes(fresh_output) == b"fresh"
            assert len(fresh_marker.read_text().splitlines()) == 1
            resumed.cancel()


if __name__ == "__main__":
    pytest_bazel.main()
