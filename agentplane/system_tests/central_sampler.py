"""A sampler Pod connected to the disposable k3s stack's real service boundaries.

This fixture runs the Go sampler binary with real TokenReview. Fixture mode omits delegated
host cgroups; the production entrypoint requires them.
"""

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
from pydantic import SecretStr

from agentplane.crd_group import GROUP, VERSION
from agentplane.grpc.calls import dial
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.settings import RUNNER_INGRESS_AUDIENCE
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2_grpc
from agentplane.runner_gateway.settings import CentralSamplerTarget
from agentplane.sandbox_resource import SANDBOX_API_VERSION
from agentplane.system_tests.cluster import api_client, kubectl
from agentplane.system_tests.conftest import namespace
from agentplane.x.central_sampler import sampler_pb2_grpc
from agentplane.x.central_sampler.testing_config import Provider, Settings
from util.testing.undeclared_outputs import undeclared_outputs_dir

SERVICE = "test-central-sampler"
MODEL = "test-central-model"
PROVIDER = "test-central-provider"
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


async def _configure_services(core: k8s.CoreV1Api, target: str) -> None:
    # Only this scenario's freshly rendered stack is patched. The address is allocated by the
    # node's gRPC listener, so it cannot be rendered into the build-time fixture chart.
    gateway = await core.read_namespaced_config_map("runner-gateway-settings", namespace())
    settings = yaml.safe_load(gateway.data["settings.yaml"])
    settings["central_samplers"] = {
        SERVICE: CentralSamplerTarget(
            target=target, token_file=Path("/var/run/secrets/test-central-sampler/token")
        ).model_dump(mode="json")
    }
    settings["executor_callers"] = [f"{namespace()}/{SERVICE}"]
    gateway.data["settings.yaml"] = yaml.safe_dump(settings)
    await core.replace_namespaced_config_map(gateway.metadata.name, namespace(), gateway)
    await kubectl(
        "patch",
        "deployment/runner-gateway",
        "--type=strategic",
        "-p",
        json.dumps(
            {
                "spec": {
                    # This single-node fixture lacks xt_statistic, which kube-proxy needs
                    # for the multiple endpoints present during a rolling update.
                    "strategy": {"type": "Recreate", "rollingUpdate": None},
                    "template": {
                        "spec": {
                            "volumes": [
                                {
                                    "name": SERVICE,
                                    "projected": {
                                        "sources": [{"serviceAccountToken": {"audience": SERVICE, "path": "token"}}]
                                    },
                                }
                            ],
                            "containers": [
                                {
                                    "name": "runner-gateway",
                                    "volumeMounts": [
                                        {
                                            "name": SERVICE,
                                            "mountPath": "/var/run/secrets/test-central-sampler",
                                            "readOnly": True,
                                        }
                                    ],
                                }
                            ],
                        }
                    },
                }
            }
        ),
    )
    history = await core.read_namespaced_config_map("history-service-config", namespace())
    settings = yaml.safe_load(history.data["config.yaml"])
    settings["ingress"]["sampler_accounts"] = [{"namespace": namespace(), "name": SERVICE}]
    history.data["config.yaml"] = yaml.safe_dump(settings)
    await core.replace_namespaced_config_map(history.metadata.name, namespace(), history)
    await kubectl("rollout", "restart", "deployment/history-service")
    await asyncio.gather(
        kubectl("rollout", "status", "deployment/runner-gateway", "--timeout=180s"),
        kubectl("rollout", "status", "deployment/history-service", "--timeout=180s"),
    )


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


async def _sampler_diagnostics(core: k8s.CoreV1Api) -> None:
    output = undeclared_outputs_dir()
    state = await kubectl("get", f"pod/{SERVICE}", "-o", "json")
    (output / "central-sampler-pod.json").write_text(state)
    for container in ("initialize", "sampler"):
        try:
            log = await core.read_namespaced_pod_log(SERVICE, namespace(), container=container)
        except ApiException as error:
            log = f"Container logs unavailable: {error}"
        (output / f"central-sampler-{container}.log").write_text(log)
    # The test owns this k3s node; preserve the Pod's EmptyDir before deletion removes it.
    uid = json.loads(state)["metadata"]["uid"]
    logs = Path(f"/var/lib/kubelet/pods/{uid}/volumes/kubernetes.io~empty-dir/logs")
    if await asyncio.to_thread(logs.exists):
        await asyncio.to_thread(shutil.copytree, logs, output / "central-sampler", dirs_exist_ok=True)


async def _delete_sampler() -> None:
    await kubectl("delete", f"pod/{SERVICE}", "--wait=true", "--timeout=60s")


async def _create_sampler(connection: k8s.ApiClient, config: Settings) -> None:
    core = k8s.CoreV1Api(connection)
    await core.create_namespaced_service_account(
        namespace(), k8s.V1ServiceAccount(metadata=k8s.V1ObjectMeta(name=SERVICE))
    )
    rbac = k8s.RbacAuthorizationV1Api(connection)
    await rbac.create_cluster_role(
        k8s.V1ClusterRole(
            metadata=k8s.V1ObjectMeta(name=SERVICE),
            rules=[
                k8s.V1PolicyRule(api_groups=["authentication.k8s.io"], resources=["tokenreviews"], verbs=["create"])
            ],
        )
    )
    await rbac.create_cluster_role_binding(
        k8s.V1ClusterRoleBinding(
            metadata=k8s.V1ObjectMeta(name=SERVICE),
            role_ref=k8s.V1RoleRef(api_group="rbac.authorization.k8s.io", kind="ClusterRole", name=SERVICE),
            subjects=[k8s.RbacV1Subject(kind="ServiceAccount", name=SERVICE, namespace=namespace())],
        )
    )
    values = config.model_dump(mode="json", exclude_none=True)
    for name, provider in config.providers.items():
        values["providers"][name]["credential"] = provider.credential.get_secret_value()
    await core.create_namespaced_config_map(
        namespace(),
        k8s.V1ConfigMap(metadata=k8s.V1ObjectMeta(name=SERVICE), data={"settings.json": json.dumps(values)}),
    )
    image = "git.allegedly.works/ducktape-ci/agentplane-central-sampler:unset"
    settings_mount = k8s.V1VolumeMount(name="settings", mount_path="/etc/central-sampler", read_only=True)
    await core.create_namespaced_pod(
        namespace(),
        k8s.V1Pod(
            metadata=k8s.V1ObjectMeta(name=SERVICE, labels={"app.kubernetes.io/name": SERVICE}),
            spec=k8s.V1PodSpec(
                service_account_name=SERVICE,
                automount_service_account_token=True,
                security_context=k8s.V1PodSecurityContext(
                    run_as_non_root=True, run_as_user=1000, run_as_group=1000, fs_group=1000
                ),
                init_containers=[
                    k8s.V1Container(
                        name="initialize",
                        image=image,
                        image_pull_policy="Never",
                        args=["--config", "/etc/central-sampler/settings.json", "--initialize"],
                        volume_mounts=[settings_mount],
                    )
                ],
                containers=[
                    k8s.V1Container(
                        name="sampler",
                        image=image,
                        image_pull_policy="Never",
                        # This scenario still omits per-host delegated cgroups.
                        args=["--config", "/etc/central-sampler/settings.json", "--fixture"],
                        ports=[k8s.V1ContainerPort(name="grpc", container_port=8080)],
                        readiness_probe=k8s.V1Probe(grpc=k8s.V1GRPCAction(port=8080), period_seconds=1),
                        resources=k8s.V1ResourceRequirements(
                            requests={"cpu": "100m", "memory": "128Mi"}, limits={"memory": "2Gi"}
                        ),
                        volume_mounts=[
                            settings_mount,
                            k8s.V1VolumeMount(name="logs", mount_path="/var/log/central-sampler"),
                            k8s.V1VolumeMount(
                                name="credentials", mount_path="/var/run/secrets/central-sampler", read_only=True
                            ),
                        ],
                    )
                ],
                volumes=[
                    k8s.V1Volume(name="settings", config_map=k8s.V1ConfigMapVolumeSource(name=SERVICE)),
                    k8s.V1Volume(name="logs", empty_dir=k8s.V1EmptyDirVolumeSource()),
                    k8s.V1Volume(
                        name="credentials",
                        projected=k8s.V1ProjectedVolumeSource(
                            sources=[
                                k8s.V1VolumeProjection(
                                    service_account_token=k8s.V1ServiceAccountTokenProjection(
                                        path=path, audience=audience, expiration_seconds=3600
                                    )
                                )
                                for path, audience in (
                                    ("gateway", "agentplane-runner-gateway"),
                                    ("history", RUNNER_INGRESS_AUDIENCE),
                                )
                            ]
                        ),
                    ),
                ],
            ),
        ),
    )


@asynccontextmanager
async def central_stack(directory: Path, model_origin: str, *, template: str) -> AsyncIterator[CentralStack]:
    async with AsyncExitStack() as cleanup, AsyncExitStack() as stack:
        stack.push_async_callback(_diagnostics)
        connection = await stack.enter_async_context(api_client())
        core = k8s.CoreV1Api(connection)
        app = await _pod(core, "app")
        caller_token = await _bound_token(core, "app", app, "session-dispatcher")
        reader_token = directory / "history-reader-token"
        reader_token.write_text(await _bound_token(core, "app", app, HISTORY_READER_AUDIENCE))
        database_name = "central_sampler_test_" + uuid4().hex
        for statement in (
            f"CREATE ROLE {database_name} LOGIN PASSWORD 'test-central-database-password'",
            f"CREATE DATABASE {database_name} OWNER {database_name}",
        ):
            await kubectl(
                "exec",
                "deployment/postgres",
                "--",
                "psql",
                "--host=/tmp",
                "--username=fixture_admin",
                "--dbname=postgres",
                "--set=ON_ERROR_STOP=1",
                "--command",
                statement,
            )
        database = await _address(core, "postgres-primary", 5432)
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
        history_target = await _address(core, "history-service", 8081)
        service = await core.create_namespaced_service(
            namespace(),
            k8s.V1Service(
                metadata=k8s.V1ObjectMeta(name=SERVICE),
                spec=k8s.V1ServiceSpec(
                    selector={"app.kubernetes.io/name": SERVICE},
                    ports=[k8s.V1ServicePort(name="grpc", port=8080, target_port=8080)],
                ),
            ),
        )
        target = f"{service.spec.cluster_ip}:8080"
        await _configure_services(core, target)
        gateway_pod = await _pod(core, "runner-gateway")
        await kubectl(
            "wait",
            f"endpointslice/{sandbox}-control",
            f"--for=jsonpath={{.endpoints[0].addresses[0]}}={gateway_pod.status.pod_ip}",
            "--timeout=60s",
        )
        # The per-Sandbox Service selects the replica holding this Sandbox's Runner stream.
        control_target = await _control_address(connection, sandbox, uid)
        config = Settings(
            database_url=f"postgresql://{database_name}:test-central-database-password@{database}/{database_name}?sslmode=disable",
            listen="0.0.0.0:8080",
            service_name=SERVICE,
            providers={
                PROVIDER: Provider(
                    endpoint=model_origin + "/v1/responses",
                    credential=SecretStr("test-provider-key"),
                    models={MODEL},
                    reasoning_efforts={""},
                )
            },
            audience=SERVICE,
            allowed_accounts={f"{namespace()}/runner-gateway"},
            code_mode_host=Path("/usr/local/bin/codex-code-mode-host"),
            executor_codex="/usr/bin/codex",
            logs=Path("/var/log/central-sampler"),
            gateway_target=control_target,
            gateway_token_file=Path("/var/run/secrets/central-sampler/gateway"),
            history_target=history_target,
            history_token_file=Path("/var/run/secrets/central-sampler/history"),
            max_live_sessions=1,
            host_rss_bytes=1024 * 1024 * 1024,
            turn_timeout_seconds=60,
            rpc_timeout_seconds=10,
            lease_seconds=60,
        )
        await _create_sampler(connection, config)
        stack.push_async_callback(_delete_sampler)
        stack.push_async_callback(_sampler_diagnostics, core)
        await kubectl("wait", f"pod/{SERVICE}", "--for=condition=Ready", "--timeout=60s")
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
        )


# gazelle:include_dep @pypi//protobuf


# Generated asynchronous gRPC stubs require grpcio types as a direct dependency.
# gazelle:include_dep @pypi//grpcio
