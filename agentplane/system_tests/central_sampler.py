"""A node-hosted sampler connected to the disposable k3s stack's real service boundaries.

This fixture runs the Go sampler binary with real TokenReview. Fixture mode omits delegated
host cgroups; the production entrypoint requires them.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import yaml
from kubernetes_asyncio import client as k8s
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
from agentplane.system_tests.k3s.stack import node_ip
from agentplane.x.central_sampler import sampler_pb2_grpc
from agentplane.x.central_sampler.testing import go_sampler
from agentplane.x.central_sampler.testing_config import Provider, Settings
from util.bazel.runfiles import get_required_path
from util.net import listen_tcp
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


async def _diagnostics(directory: Path) -> None:
    output = undeclared_outputs_dir()
    (output / "central-services.json").write_text(await kubectl("get", "services,endpointslices,pods", "-o", "json"))
    for component in ("runner-gateway", "history-service"):
        (output / f"central-{component}.log").write_text(
            await kubectl("logs", f"deployment/{component}", "--all-containers")
        )
    sampler_stderr = directory / "sampler.log"
    if sampler_stderr.exists():
        (output / "central-sampler-stderr.log").write_text(sampler_stderr.read_text())


@asynccontextmanager
async def central_stack(directory: Path, model_origin: str, *, template: str) -> AsyncIterator[CentralStack]:
    async with AsyncExitStack() as cleanup, AsyncExitStack() as stack:
        stack.push_async_callback(_diagnostics, directory)
        connection = await stack.enter_async_context(api_client())
        core = k8s.CoreV1Api(connection)
        await core.create_namespaced_service_account(
            namespace(), k8s.V1ServiceAccount(metadata=k8s.V1ObjectMeta(name=SERVICE))
        )
        app = await _pod(core, "app")
        identity = await core.create_namespaced_pod(
            namespace(),
            k8s.V1Pod(
                metadata=k8s.V1ObjectMeta(name=SERVICE),
                spec=k8s.V1PodSpec(
                    service_account_name=SERVICE,
                    automount_service_account_token=False,
                    containers=[
                        k8s.V1Container(
                            name="identity",
                            # The identity Pod only needs sleep; reuse the imported Debian-based app image.
                            image=next(container.image for container in app.spec.containers if container.name == "app"),
                            image_pull_policy="Never",
                            command=["/bin/sleep", "infinity"],
                        )
                    ],
                ),
            ),
        )
        await kubectl("wait", f"pod/{SERVICE}", "--for=condition=Ready", "--timeout=60s")
        gateway_token = directory / "executor-token"
        gateway_token.write_text(await _bound_token(core, SERVICE, identity, "agentplane-runner-gateway"))
        writer_token = directory / "history-writer-token"
        writer_token.write_text(await _bound_token(core, SERVICE, identity, RUNNER_INGRESS_AUDIENCE))
        caller_token = await _bound_token(core, "app", app, "session-dispatcher")
        reader_token = directory / "history-reader-token"
        reader_token.write_text(await _bound_token(core, "app", app, HISTORY_READER_AUDIENCE))
        for statement in (
            "CREATE ROLE central_sampler_test LOGIN PASSWORD 'test-central-database-password'",
            "CREATE DATABASE central_sampler_test OWNER central_sampler_test",
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
        # The per-Sandbox Service points to the replica holding that Sandbox's Runner stream.
        control_target = await _control_address(connection, sandbox, uid)
        history_target = await _address(core, "history-service", 8081)
        config = Settings(
            database_url=f"postgresql://central_sampler_test:test-central-database-password@{database}/central_sampler_test",
            listen=f"{node_ip()}:0",
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
            code_mode_host=get_required_path("agentplane_codex_cli_linux_x64/bin/codex-code-mode-host"),
            executor_codex="/usr/bin/codex",
            logs=undeclared_outputs_dir() / "central-sampler",
            gateway_target=control_target,
            gateway_token_file=gateway_token,
            history_target=history_target,
            history_token_file=writer_token,
            max_live_sessions=1,
            host_rss_bytes=1024 * 1024 * 1024,
            turn_timeout_seconds=60,
            rpc_timeout_seconds=10,
            lease_seconds=60,
        )
        config.logs.mkdir(parents=True)
        listener = stack.enter_context(listen_tcp("0.0.0.0"))
        target = f"{node_ip()}:{listener.port}"
        await _configure_services(core, target)
        gateway_pod = await _pod(core, "runner-gateway")
        await kubectl(
            "wait",
            f"endpointslice/{sandbox}-control",
            f"--for=jsonpath={{.endpoints[0].addresses[0]}}={gateway_pod.status.pod_ip}",
            "--timeout=60s",
        )
        control_target = await _control_address(connection, sandbox, uid)
        config.gateway_target = control_target
        await stack.enter_async_context(
            go_sampler(config, Path(os.environ["KUBECONFIG"]), directory, listener=listener, advertised_host=node_ip())
        )
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
