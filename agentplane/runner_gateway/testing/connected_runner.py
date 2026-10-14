"""The Runner Gateway over a fake Kubernetes cluster, with a runner connected to the gateway."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from kubernetes_asyncio import client as k8s_client

from agentplane.grpc_calls import dial
from agentplane.runner.runner import Runner
from agentplane.runner_gateway.client import RunnerGatewayClient
from agentplane.runner_gateway.destinations import DestinationResolver
from agentplane.runner_gateway.grpc_api import Resources
from agentplane.runner_gateway.testing.control_listener import control_plane, dialing, wait_connected
from agentplane.runner_gateway.testing.grpc_service import gateway_client, service
from agentplane.runner_gateway.testing.resources import build_resources
from agentplane.sandboxes.testing.kubernetes import ACCOUNT, POD_UID, SANDBOX, SANDBOX_UID, SandboxCluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from util.testing.sqlalchemy_engines import postgres_async_engine

_RUNNER_AUDIENCE = "test-app-runner-control"
_RUNNER_TOKEN = "test-app-runner-token"


@asynccontextmanager
async def authenticated_runner_gateway(
    cluster: SandboxCluster,
    runner: Runner,
    token_file: Path,
    *,
    database_url: str,
    manager: ServiceAccountRef | None = None,
    token: str = "test-app-gateway-token",
    audience: str = "test-app-runner-gateway",
) -> AsyncIterator[RunnerGatewayClient]:
    """The Runner Gateway over `cluster`, with `runner` connected over the control stream."""
    manager = manager or ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="test-app")
    await asyncio.to_thread(token_file.write_text, token)
    cluster.fake.tokens[token] = TokenVerdict(
        username=f"system:serviceaccount:{manager.namespace}:{manager.name}",
        pod_name="test-app-pod",
        pod_uid="test-app-pod-uid",
        audiences=(audience,),
    )
    async with postgres_async_engine(database_url) as engine:
        resources = build_resources(
            engine,
            principals=cluster.build_principal_resolver(audience),
            destinations=DestinationResolver(cluster.inventory, k8s_client.CoreV1Api(cluster.api)),
            caller_accounts=frozenset({manager}),
            # Native acceptance uses production's admission budget, including delivery's
            # two-second recheck. The gateway unit fixture keeps its one-second default.
            runner_admission_ack_timeout_s=15,
        )
        async with runner_connected(cluster, resources, runner), service(resources) as target, dial(target) as channel:
            yield gateway_client(
                channel,
                token_file,
                command_admission_timeout_s=resources.runner_admission_ack_timeout_s + 10,
                control_target=target,
            )


@asynccontextmanager
async def runner_connected(cluster: SandboxCluster, resources: Resources, runner: Runner) -> AsyncIterator[None]:
    """`runner`, as `SANDBOX`'s, holds a control stream to the gateway `resources` back."""
    cluster.fake.tokens[_RUNNER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{ACCOUNT}",
        pod_name=SANDBOX,
        pod_uid=POD_UID,
        audiences=(_RUNNER_AUDIENCE,),
    )
    async with (
        control_plane(
            resources, principals=cluster.build_principal_resolver(_RUNNER_AUDIENCE), api=cluster.api
        ) as target,
        dialing(runner, target, _RUNNER_TOKEN),
    ):
        await wait_connected(resources, UUID(SANDBOX_UID))
        yield
