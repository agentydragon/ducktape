"""A chart-deployed sampler Pod connected to the disposable k3s stack's service boundaries."""

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import yaml
from kubernetes_asyncio import client as k8s
from kubernetes_asyncio.client.exceptions import ApiException

from agentplane.crd_group import GROUP, VERSION
from agentplane.grpc.calls import dial
from agentplane.history_service.client import HistoryServiceClient
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2_grpc
from agentplane.sandbox_resource import SANDBOX_API_VERSION
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.tracing import tracer
from agentplane.x.central_sampler import sampler_pb2_grpc
from model_catalog.catalog import GPT6_LUNA_RESPONSES
from util.testing.undeclared_outputs import undeclared_outputs_dir

SERVICE = "central-sampler"
MODEL = GPT6_LUNA_RESPONSES.id
HISTORY_READER_AUDIENCE = "agentplane-history-service"


@dataclass(frozen=True)
class CentralStack:
    dispatcher: session_dispatcher_pb2_grpc.SessionDispatcherAsyncStub
    metadata: tuple[tuple[str, str], ...] = field(repr=False)
    sampler: sampler_pb2_grpc.SamplerAsyncStub
    sampler_metadata: tuple[tuple[str, str], ...] = field(repr=False)
    history: HistoryServiceClient
    sandbox: str
    sandbox_uid: str
    incarnation_uid: str
    control_target: str
    sampler_pod_name: str


async def _bound_token(cluster: Cluster, core: k8s.CoreV1Api, account: str, pod: k8s.V1Pod, audience: str) -> str:
    reply = await core.create_namespaced_service_account_token(
        account,
        cluster.namespace,
        k8s.AuthenticationV1TokenRequest(
            spec=k8s.V1TokenRequestSpec(
                audiences=[audience],
                expiration_seconds=3600,
                bound_object_ref=k8s.V1BoundObjectReference(
                    api_version="v1", kind="Pod", name=pod.metadata.name, uid=pod.metadata.uid
                ),
            )
        ),
    )
    return reply.status.token


async def _pod(cluster: Cluster, core: k8s.CoreV1Api, component: str) -> k8s.V1Pod:
    pods = await core.list_namespaced_pod(cluster.namespace, label_selector=f"app.kubernetes.io/name={component}")
    return next(pod for pod in pods.items if pod.metadata.deletion_timestamp is None)


async def _address(cluster: Cluster, core: k8s.CoreV1Api, service: str, port: int) -> str:
    endpoint = await core.read_namespaced_service(service, cluster.namespace)
    assert endpoint.spec.cluster_ip not in (None, "None"), service
    return f"{endpoint.spec.cluster_ip}:{port}"


async def _control_address(cluster: Cluster, connection: k8s.ApiClient, sandbox: str, uid: str) -> str:
    # The control Service is headless and selector-less. Its Gateway-owned slice names
    # the Runner observation Lease holder, including after the scenario's Gateway rollout.
    slices = await k8s.DiscoveryV1Api(connection).list_namespaced_endpoint_slice(
        cluster.namespace, label_selector=f"kubernetes.io/service-name={sandbox}-control"
    )
    (holder,) = slices.items
    assert holder.metadata.labels["endpointslice.kubernetes.io/managed-by"] == "agentplane-runner-gateway"
    assert any(owner.uid == uid for owner in holder.metadata.owner_references)
    (endpoint,) = holder.endpoints
    assert endpoint.conditions.ready
    (address,) = endpoint.addresses
    (port,) = holder.ports
    assert port.protocol == "TCP"
    return f"{address}:{port.port}"


async def _delete_sandbox(cluster: Cluster, name: str, uid: str) -> None:
    with tracer.start_as_current_span(
        "system_test.central_sampler.sandbox_cleanup", record_exception=False, set_status_on_exception=False
    ):
        await cluster.kubectl(
            "patch",
            "sandbox",
            name,
            "--type=merge",
            "-p",
            json.dumps({"metadata": {"uid": uid}, "spec": {"running": False}}),
        )
        await cluster.kubectl(
            "wait", f"sandbox/{name}", "--for=jsonpath={.status.operatingMode}=Suspended", "--timeout=300s"
        )
        await cluster.kubectl("wait", f"sandbox/{name}", "--for=jsonpath={.status.stopStage}=Stopped", "--timeout=300s")
        async with cluster.api_client() as connection:
            await k8s.CustomObjectsApi(connection).delete_namespaced_custom_object(
                GROUP,
                VERSION,
                cluster.namespace,
                "sandboxes",
                name,
                body=k8s.V1DeleteOptions(preconditions=k8s.V1Preconditions(uid=uid)),
            )
        await cluster.kubectl("wait", f"sandbox/{name}", "--for=delete", "--timeout=120s")


async def _diagnostics(cluster: Cluster) -> None:
    with tracer.start_as_current_span(
        "system_test.central_sampler.diagnostics", record_exception=False, set_status_on_exception=False
    ):
        output = undeclared_outputs_dir()
        (output / "central-services.json").write_text(
            await cluster.kubectl("get", "services,endpointslices,pods", "-o", "json")
        )
        for component in ("runner-gateway", "history-service"):
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
            app = await _pod(cluster, core, "app")
            caller_token = await _bound_token(cluster, core, "app", app, "session-dispatcher")
            reader_token = directory / "history-reader-token"
            reader_token.write_text(await _bound_token(cluster, core, "app", app, HISTORY_READER_AUDIENCE))
            app_config = await core.read_namespaced_config_map("app-config", cluster.namespace)
            policies = yaml.safe_load(app_config.data["config.yaml"])["default_policy_sets"]
            sandbox = "systest-central-" + uuid4().hex[:8]
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
                        "metadata": {"name": sandbox, "namespace": cluster.namespace},
                        "spec": {"template": template, "running": True, "policySets": policies},
                    },
                )
            )
            uid = created["metadata"]["uid"]
            cleanup.push_async_callback(_delete_sandbox, cluster, sandbox, uid)
            await cluster.kubectl("wait", f"sandbox/{sandbox}", "--for=condition=Ready=True", "--timeout=300s")
            ready = json.loads(await cluster.kubectl("get", "sandbox", sandbox, "-o", "json"))
            incarnation = ready["status"]["incarnation"]["uid"]
            target = await _address(cluster, core, SERVICE, 8080)
            sampler_pod = await _pod(cluster, core, SERVICE)
            stack.push_async_callback(capture_sampler_diagnostics, cluster, core)
            gateway_pod = await _pod(cluster, core, "runner-gateway")
            await cluster.kubectl(
                "wait",
                f"endpointslice/{sandbox}-control",
                f"--for=jsonpath={{.endpoints[0].addresses[0]}}={gateway_pod.status.pod_ip}",
                "--timeout=60s",
            )
            # The per-Sandbox Service selects the replica holding this Sandbox's Runner stream.
            control_target = await _control_address(cluster, connection, sandbox, uid)
            sampler_token = await _bound_token(cluster, core, "runner-gateway", gateway_pod, SERVICE)
            sampler_channel = await stack.enter_async_context(dial(target))
            dispatcher_channel = await stack.enter_async_context(
                dial(await _address(cluster, core, "session-dispatcher", 8080))
            )
            await asyncio.wait_for(dispatcher_channel.channel_ready(), timeout=30)
            history = await stack.enter_async_context(
                HistoryServiceClient(
                    await _address(cluster, core, "history-service", 8080),
                    token_file=reader_token,
                    request_timeout_s=20,
                )
            )
            deployed = CentralStack(
                dispatcher=session_dispatcher_pb2_grpc.SessionDispatcherStub(dispatcher_channel),
                metadata=(("authorization", f"Bearer {caller_token}"),),
                sampler=sampler_pb2_grpc.SamplerStub(sampler_channel),
                sampler_metadata=(("authorization", f"Bearer {sampler_token}"),),
                history=history,
                sandbox=sandbox,
                sandbox_uid=uid,
                incarnation_uid=incarnation,
                control_target=control_target,
                sampler_pod_name=sampler_pod.metadata.name,
            )
        yield deployed


# gazelle:include_dep @pypi//protobuf


# Generated asynchronous gRPC stubs require grpcio types as a direct dependency.
# gazelle:include_dep @pypi//grpcio
