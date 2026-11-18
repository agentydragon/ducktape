"""A chart-deployed sampler Pod connected to the disposable k3s stack's service boundaries."""

import asyncio
import base64
import json
import shutil
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from uuid import uuid4

import grpc
from kubernetes_asyncio import client as k8s
from kubernetes_asyncio.client.exceptions import ApiException

from agentplane.grpc.calls import ServiceError, dial
from agentplane.history_service.client import HistoryServiceClient
from agentplane.proto.identity_pb2 import EnvironmentTarget, SandboxDestination, ServiceAccount
from agentplane.sampler.api import sampler_admin_pb2_grpc
from agentplane.sandbox_resource import SANDBOX_API_VERSION
from agentplane.session_dispatcher.client import SandboxControlClient, SandboxRunner
from agentplane.session_dispatcher.dispatcher.api.session import session_dispatcher_pb2_grpc
from agentplane.system_tests.driver import sandbox_cleanup
from agentplane.system_tests.driver.cleanup_diagnostics import cleanup_without_masking
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.identity import (
    LAUNCH_DEFAULT_POLICY_SETS,
    create_scenario_identity,
    delete_scenario_identity,
)
from agentplane.system_tests.driver.native_auth import RUNTIME_PROXY_TEST_READER_SECRET
from agentplane.system_tests.driver.sandbox_ownership import release_owned_sandboxes, remember_sandbox
from agentplane.system_tests.driver.tls import service_svid_credentials
from agentplane.system_tests.driver.tracing import tracer
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from model_catalog.catalog import GPT6_LUNA_RESPONSES

SERVICE = "central-sampler"
MODEL = GPT6_LUNA_RESPONSES.id


@dataclass(frozen=True)
class CentralStack:
    dispatcher: session_dispatcher_pb2_grpc.SessionDispatcherAsyncStub
    metadata: tuple[tuple[str, str], ...] = field(repr=False)
    sampler_admin: sampler_admin_pb2_grpc.SamplerAdminAsyncStub
    sampler_metadata: tuple[tuple[str, str], ...] = field(repr=False)
    history: HistoryServiceClient
    sandbox: str
    sandbox_uid: str
    incarnation_uid: str
    gateway_target: str
    sampler_pod_name: str
    runner: SandboxRunner

    async def wait_for_runner(self, incarnation_uid: str) -> None:
        """Prove the Gateway has a callable connection to this workload, without replaying mutations."""
        async with asyncio.timeout(15):
            while True:
                try:
                    await self.runner.list_sessions(expected_incarnation_uid=incarnation_uid)
                    return
                except ServiceError as error:
                    if error.code not in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.ABORTED):
                        raise
                await asyncio.sleep(0.05)


async def _pod(cluster: Cluster, core: k8s.CoreV1Api, component: str) -> k8s.V1Pod:
    pods = await core.list_namespaced_pod(cluster.namespace, label_selector=f"app.kubernetes.io/name={component}")
    return next(pod for pod in pods.items if pod.metadata.deletion_timestamp is None)


async def _address(cluster: Cluster, core: k8s.CoreV1Api, service: str, port: int) -> str:
    endpoint = await core.read_namespaced_service(service, cluster.namespace)
    assert endpoint.spec.cluster_ip not in (None, "None"), service
    return f"{endpoint.spec.cluster_ip}:{port}"


@asynccontextmanager
async def _owned_native_sandbox(cluster: Cluster, name: str, template: str) -> AsyncIterator[EnvironmentTarget]:
    """Own the acknowledged native Sandbox before readiness work and release its Identity last."""
    async with AsyncExitStack() as cleanup:
        identity = await create_scenario_identity(cluster, name, list(LAUNCH_DEFAULT_POLICY_SETS))
        created = json.loads(
            await cluster.kubectl(
                "create",
                "-f",
                "-",
                "-o",
                "json",
                body={
                    "apiVersion": SANDBOX_API_VERSION,
                    "kind": "Sandbox",
                    "metadata": {"name": name, "namespace": cluster.namespace},
                    "spec": {"template": template, "running": True, "identity": {"name": identity["name"]}},
                },
            )
        )
        metadata = created["metadata"]
        target = EnvironmentTarget(namespace=metadata["namespace"], name=metadata["name"], uid=metadata["uid"])
        owned: list[EnvironmentTarget] = []
        remember_sandbox(owned, target)

        async def release(target: EnvironmentTarget) -> None:
            await sandbox_cleanup.delete_sandbox(cluster, target.name, target.uid)

        async def release_owned(
            _exc_type: type[BaseException] | None, exc: BaseException | None, _traceback: TracebackType | None
        ) -> bool:
            await cleanup_without_masking(exc, lambda: release_owned_sandboxes(owned, release))
            return False

        async def delete_identity(
            _exc_type: type[BaseException] | None, exc: BaseException | None, _traceback: TracebackType | None
        ) -> bool:
            await cleanup_without_masking(exc, lambda: delete_scenario_identity(cluster, identity))
            return False

        # Register only after create returned the exact Sandbox UID. An unknown create outcome
        # leaves the Identity for recovery because that Sandbox may already reference it.
        cleanup.push_async_exit(delete_identity)
        cleanup.push_async_exit(release_owned)
        yield target


async def _diagnostics(cluster: Cluster) -> None:
    with tracer.start_as_current_span(
        "system_test.central_sampler.diagnostics", record_exception=False, set_status_on_exception=False
    ):
        output = undeclared_outputs_dir()
        state = await cluster.kubectl("get", "services,endpointslices,pods", "-o", "json")
        (output / "central-services.json").write_text(state)
        sandbox_pods = [
            pod
            for pod in json.loads(state)["items"]
            if pod.get("kind") == "Pod"
            and pod.get("metadata", {}).get("labels", {}).get("agentplane.allegedly.works/sandbox")
        ]
        if sandbox_pods:
            async with cluster.api_client() as connection:
                core = k8s.CoreV1Api(connection)
                for pod in sandbox_pods:
                    name = pod["metadata"]["name"]
                    pod_output = output / "sandbox-pods" / name
                    pod_output.mkdir(parents=True, exist_ok=True)
                    (pod_output / "pod.json").write_text(json.dumps(pod, indent=2) + "\n")
                    for container in pod.get("spec", {}).get("containers", []):
                        container_name = container["name"]
                        try:
                            log = await core.read_namespaced_pod_log(name, cluster.namespace, container=container_name)
                        except ApiException as error:
                            log = f"Container logs unavailable: {error}"
                        (pod_output / f"{container_name}.log").write_text(log)
        for component in ("sessions", "history-service"):
            (output / f"central-{component}.log").write_text(
                await cluster.kubectl("logs", f"deployment/{component}", "--all-containers")
            )


async def capture_sampler_diagnostics(cluster: Cluster, core: k8s.CoreV1Api | None = None) -> Path:
    """Retain this sampler Pod's logs before another scenario replaces the Pod."""
    if core is None:
        async with cluster.api_client() as connection:
            return await capture_sampler_diagnostics(cluster, k8s.CoreV1Api(connection))

    with tracer.start_as_current_span(
        "system_test.central_sampler.capture_diagnostics", record_exception=False, set_status_on_exception=False
    ):
        pod = await _pod(cluster, core, SERVICE)
        state = await cluster.kubectl("get", "pod", pod.metadata.name, "-o", "json")
        uid = str(pod.metadata.uid)
        output = undeclared_outputs_dir() / "central-sampler-pods" / uid
        output.mkdir(parents=True, exist_ok=True)
        (output / "pod.json").write_text(state)
        containers = [container.name for container in pod.spec.containers]
        containers.extend(container.name for container in pod.spec.init_containers or [])
        for container in containers:
            try:
                log = await core.read_namespaced_pod_log(pod.metadata.name, cluster.namespace, container=container)
            except ApiException as error:
                log = f"Container logs unavailable: {error}"
            (output / f"{container}.log").write_text(log)
        # Preserve the Pod's EmptyDir before the disposable k3s stack tears it down.
        logs = Path(f"/var/lib/kubelet/pods/{uid}/volumes/kubernetes.io~empty-dir/logs")
        if await asyncio.to_thread(logs.exists):
            await asyncio.to_thread(shutil.copytree, logs, output / "sessions", dirs_exist_ok=True)
        return output


@asynccontextmanager
async def central_stack(cluster: Cluster, directory: Path, *, template: str) -> AsyncIterator[CentralStack]:
    async with AsyncExitStack() as cleanup, AsyncExitStack() as stack:
        with tracer.start_as_current_span(
            "system_test.central_sampler.setup", record_exception=False, set_status_on_exception=False
        ):
            stack.push_async_callback(_diagnostics, cluster)
            connection = await stack.enter_async_context(cluster.api_client())
            core = k8s.CoreV1Api(connection)
            sandbox = "systest-central-" + uuid4().hex[:8]
            sandbox_target = await cleanup.enter_async_context(_owned_native_sandbox(cluster, sandbox, template))
            sandbox, uid = sandbox_target.name, sandbox_target.uid
            with tracer.start_as_current_span(
                "system_test.central_sampler.sandbox.ready",
                attributes={"sandbox.name": sandbox, "sandbox.uid": uid, "sandbox.template": template},
            ):
                await cluster.kubectl("wait", f"sandbox/{sandbox}", "--for=condition=Ready=True", "--timeout=300s")
            ready = json.loads(await cluster.kubectl("get", "sandbox", sandbox, "-o", "json"))
            incarnation = ready["status"]["incarnation"]["uid"]
            sampler_target = await _address(cluster, core, SERVICE, 8083)
            sampler_pod = await _pod(cluster, core, SERVICE)
            stack.push_async_callback(capture_sampler_diagnostics, cluster, core)
            # Call through the stable Service so Kubernetes can send every request to any replica.
            gateway_target = await _address(cluster, core, "environments", 8082)
            # Direct observations act as the same Sessions service principal as Gateway.
            # Its certificate survives a Gateway Pod roll; no Pod-bound bearer is involved.
            sampler_client = await core.read_namespaced_secret("sessions-client-workload-tls", cluster.namespace)
            sampler_trust = await core.read_namespaced_config_map("agentplane-workload-ca-bundle", cluster.namespace)
            sampler_credentials = grpc.ssl_channel_credentials(
                root_certificates=sampler_trust.data["ca-certificates.crt"].encode(),
                private_key=base64.b64decode(sampler_client.data["tls.key"]),
                certificate_chain=base64.b64decode(sampler_client.data["tls.crt"]),
            )
            # Sandbox Ready proves leader observation; independently prove the every-replica
            # Environments proxy has a callable snapshot before starting setup.
            # Dispatcher commands and History observations use App; raw reads use the fixture-only reader.
            caller_credentials = await service_svid_credentials(cluster, "app-workload-tls")
            gateway = SandboxControlClient(
                control_target=gateway_target,
                token_file=None,
                request_timeout_s=2,
                channel_credentials=await service_svid_credentials(cluster, RUNTIME_PROXY_TEST_READER_SECRET),
                server_name=f"environments.{cluster.namespace}.svc.cluster.local",
            )
            destination = SandboxDestination(
                owner=ServiceAccount(namespace=cluster.namespace, name=sandbox),
                target=EnvironmentTarget(namespace=cluster.namespace, name=sandbox, uid=uid),
            )
            sampler_channel = await stack.enter_async_context(
                dial(
                    sampler_target,
                    channel_credentials=sampler_credentials,
                    server_name=f"{SERVICE}.{cluster.namespace}.svc.cluster.local",
                )
            )
            dispatcher_channel = await stack.enter_async_context(
                dial(
                    await _address(cluster, core, "sessions", 8080),
                    channel_credentials=caller_credentials,
                    server_name=f"sessions.{cluster.namespace}.svc.cluster.local",
                )
            )
            await asyncio.wait_for(dispatcher_channel.channel_ready(), timeout=30)
            history = await stack.enter_async_context(
                HistoryServiceClient(
                    await _address(cluster, core, "history-service", 8080),
                    request_timeout_s=20,
                    channel_credentials=caller_credentials,
                    server_name=f"history-service.{cluster.namespace}.svc.cluster.local",
                )
            )
            deployed = CentralStack(
                dispatcher=session_dispatcher_pb2_grpc.SessionDispatcherStub(dispatcher_channel),
                metadata=(),
                sampler_admin=sampler_admin_pb2_grpc.SamplerAdminStub(sampler_channel),
                sampler_metadata=(),
                history=history,
                sandbox=sandbox,
                sandbox_uid=uid,
                incarnation_uid=incarnation,
                gateway_target=gateway_target,
                sampler_pod_name=sampler_pod.metadata.name,
                runner=gateway.runner(destination),
            )
        await deployed.wait_for_runner(incarnation)
        yield deployed


# gazelle:include_dep @pypi//protobuf


# Generated asynchronous gRPC stubs require grpcio types as a direct dependency.
# gazelle:include_dep @pypi//grpcio
