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
from agentplane.system_tests.cluster import api_client, kubectl
from agentplane.system_tests.conftest import namespace
from agentplane.x.central_sampler import sampler_pb2_grpc
from model_catalog.catalog import GPT6_LUNA_RESPONSES
from util.testing.undeclared_outputs import undeclared_outputs_dir

SERVICE = "central-sampler"
MODEL = GPT6_LUNA_RESPONSES.id
PROVIDER = "responses"
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


async def _bound_token(core: k8s.CoreV1Api, account: str, pod: k8s.V1Pod, audience: str) -> str:
    reply = await core.create_namespaced_service_account_token(
        account,
        namespace(),
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


async def _pod(core: k8s.CoreV1Api, component: str) -> k8s.V1Pod:
    pods = await core.list_namespaced_pod(namespace(), label_selector=f"app.kubernetes.io/name={component}")
    return next(pod for pod in pods.items if pod.metadata.deletion_timestamp is None)


async def _address(core: k8s.CoreV1Api, service: str, port: int) -> str:
    endpoint = await core.read_namespaced_service(service, namespace())
    assert endpoint.spec.cluster_ip not in (None, "None"), service
    return f"{endpoint.spec.cluster_ip}:{port}"


async def _control_address(connection: k8s.ApiClient, sandbox: str, uid: str) -> str:
    # The control Service is headless and selector-less. Its Gateway-owned slice names
    # the actual Runner-stream holder, including after the scenario's Gateway rollout.
    slices = await k8s.DiscoveryV1Api(connection).list_namespaced_endpoint_slice(
        namespace(), label_selector=f"kubernetes.io/service-name={sandbox}-control"
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


async def _delete_sandbox(name: str, uid: str) -> None:
    await kubectl(
        "patch",
        "sandbox",
        name,
        "--type=merge",
        "-p",
        json.dumps({"metadata": {"uid": uid}, "spec": {"running": False}}),
    )
    await kubectl("wait", f"sandbox/{name}", "--for=jsonpath={.status.operatingMode}=Suspended", "--timeout=300s")
    await kubectl(
        "wait",
        f"sandbox/{name}",
        '--for=jsonpath={.status.conditions[?(@.type=="Ready")].reason}=Stopped',
        "--timeout=300s",
    )
    async with api_client() as connection:
        await k8s.CustomObjectsApi(connection).delete_namespaced_custom_object(
            GROUP,
            VERSION,
            namespace(),
            "sandboxes",
            name,
            body=k8s.V1DeleteOptions(preconditions=k8s.V1Preconditions(uid=uid)),
        )
    await kubectl("wait", f"sandbox/{name}", "--for=delete", "--timeout=120s")


async def _diagnostics() -> None:
    output = undeclared_outputs_dir()
    (output / "central-services.json").write_text(await kubectl("get", "services,endpointslices,pods", "-o", "json"))
    for component in ("runner-gateway", "history-service"):
        (output / f"central-{component}.log").write_text(
            await kubectl("logs", f"deployment/{component}", "--all-containers")
        )


async def capture_sampler_diagnostics(core: k8s.CoreV1Api | None = None) -> Path:
    """Retain this sampler Pod's logs before another scenario replaces the Pod."""
    if core is None:
        async with api_client() as connection:
            return await capture_sampler_diagnostics(k8s.CoreV1Api(connection))

    pod = await _pod(core, SERVICE)
    state = await kubectl("get", "pod", pod.metadata.name, "-o", "json")
    uid = str(pod.metadata.uid)
    output = undeclared_outputs_dir() / "central-sampler-pods" / uid
    output.mkdir(parents=True, exist_ok=True)
    (output / "pod.json").write_text(state)
    containers = [container.name for container in pod.spec.containers]
    containers.extend(container.name for container in pod.spec.init_containers or [])
    for container in containers:
        try:
            log = await core.read_namespaced_pod_log(pod.metadata.name, namespace(), container=container)
        except ApiException as error:
            log = f"Container logs unavailable: {error}"
        (output / f"{container}.log").write_text(log)
    # Preserve the Pod's EmptyDir before the disposable k3s stack tears it down.
    logs = Path(f"/var/lib/kubelet/pods/{uid}/volumes/kubernetes.io~empty-dir/logs")
    if await asyncio.to_thread(logs.exists):
        await asyncio.to_thread(shutil.copytree, logs, output / "sessions", dirs_exist_ok=True)
    return output


@asynccontextmanager
async def central_stack(directory: Path, *, template: str) -> AsyncIterator[CentralStack]:
    async with AsyncExitStack() as cleanup, AsyncExitStack() as stack:
        stack.push_async_callback(_diagnostics)
        connection = await stack.enter_async_context(api_client())
        core = k8s.CoreV1Api(connection)
        app = await _pod(core, "app")
        caller_token = await _bound_token(core, "app", app, "session-dispatcher")
        reader_token = directory / "history-reader-token"
        reader_token.write_text(await _bound_token(core, "app", app, HISTORY_READER_AUDIENCE))
        app_config = await core.read_namespaced_config_map("app-config", namespace())
        policies = yaml.safe_load(app_config.data["config.yaml"])["default_policy_sets"]
        sandbox = "systest-central-" + uuid4().hex[:8]
        created = json.loads(
            await kubectl(
                "create",
                "-f",
                "-",
                "-o",
                "json",
                body={
                    "apiVersion": SANDBOX_API_VERSION,
                    "kind": "Sandbox",
                    "metadata": {"name": sandbox, "namespace": namespace()},
                    "spec": {"template": template, "running": True, "policySets": policies},
                },
            )
        )
        uid = created["metadata"]["uid"]
        cleanup.push_async_callback(_delete_sandbox, sandbox, uid)
        await kubectl("wait", f"sandbox/{sandbox}", "--for=condition=Ready=True", "--timeout=300s")
        ready = json.loads(await kubectl("get", "sandbox", sandbox, "-o", "json"))
        incarnation = ready["status"]["incarnation"]["uid"]
        target = await _address(core, SERVICE, 8080)
        sampler_pod = await _pod(core, SERVICE)
        stack.push_async_callback(capture_sampler_diagnostics, core)
        gateway_pod = await _pod(core, "runner-gateway")
        await kubectl(
            "wait",
            f"endpointslice/{sandbox}-control",
            f"--for=jsonpath={{.endpoints[0].addresses[0]}}={gateway_pod.status.pod_ip}",
            "--timeout=60s",
        )
        # The per-Sandbox Service selects the replica holding this Sandbox's Runner stream.
        control_target = await _control_address(connection, sandbox, uid)
        sampler_token = await _bound_token(core, "runner-gateway", gateway_pod, SERVICE)
        sampler_channel = await stack.enter_async_context(dial(target))
        dispatcher_channel = await stack.enter_async_context(dial(await _address(core, "session-dispatcher", 8080)))
        await asyncio.wait_for(dispatcher_channel.channel_ready(), timeout=30)
        history = await stack.enter_async_context(
            HistoryServiceClient(
                await _address(core, "history-service", 8080), token_file=reader_token, request_timeout_s=20
            )
        )
        yield CentralStack(
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


# gazelle:include_dep @pypi//protobuf


# Generated asynchronous gRPC stubs require grpcio types as a direct dependency.
# gazelle:include_dep @pypi//grpcio
