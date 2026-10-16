"""Run the authenticated Runner Gateway over in-memory Kubernetes for consumer tests.

The server has its own event loop, so a test can block on it (`wait_connected`) without starving gRPC.
Only the endpoint and public client escape this fixture.
"""

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID

from anyio.from_thread import start_blocking_portal
from kubernetes_asyncio import client as k8s_client

from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.runner_gateway.destinations import DestinationResolver
from agentplane.runner_gateway.testing.control_listener import control_plane, wait_connected
from agentplane.runner_gateway.testing.grpc_service import service
from agentplane.runner_gateway.testing.resources import build_resources
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.sandboxes.testing.fake_inventory import NAMESPACE, FakeCoreV1Api, FakeCustomObjectsApi
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import fake_apiserver
from agentplane.workload_auth.principal import POD_NAME_CLAIM, POD_UID_CLAIM, WorkloadPrincipalResolver
from util.kubernetes import CustomObjectsClient

TOKEN = "test-consumer-runner-gateway-token"
AUDIENCE = "test-consumer-runner-gateway"
RUNNER_AUDIENCE = "test-consumer-runner-control"
MANAGER = ServiceAccountRef(namespace=NAMESPACE, name="test-integration-app")
_RUNNER_TOKEN_PREFIX = "test-runner-token-"


def runner_bearer(sandbox_name: str) -> str:
    """The control-stream token of the runner in `seed_runner`'s Pod for `sandbox_name`."""
    return f"{_RUNNER_TOKEN_PREFIX}{sandbox_name}"


class Authentication:
    async def create_token_review(self, body: k8s_client.V1TokenReview) -> k8s_client.V1TokenReview:
        token = body.spec.token
        if token.startswith(_RUNNER_TOKEN_PREFIX):
            name = token.removeprefix(_RUNNER_TOKEN_PREFIX)
            account, pod_name, pod_uid, audience = name, name, f"test-pod-{name}", RUNNER_AUDIENCE
        else:
            account, pod_name, pod_uid, audience = MANAGER.name, "test-app-pod", "test-app-pod-uid", AUDIENCE
        return k8s_client.V1TokenReview(
            spec=body.spec,
            status=k8s_client.V1TokenReviewStatus(
                authenticated=token == TOKEN or token.startswith(_RUNNER_TOKEN_PREFIX),
                audiences=[audience],
                user=k8s_client.V1UserInfo(
                    username=f"system:serviceaccount:{NAMESPACE}:{account}",
                    uid=f"{account}-uid",
                    extra={POD_NAME_CLAIM: [pod_name], POD_UID_CLAIM: [pod_uid]},
                ),
            ),
        )


@dataclass(frozen=True)
class Endpoint:
    target: str
    token_file: Path
    # The gateway's runner control listener, which runners dial with `runner_bearer`.
    control_target: str
    wait_connected: Callable[[UUID], None]

    def client(self) -> SandboxControlClient:
        """The Runner proxy client reached through the test's local control Service."""
        return SandboxControlClient(
            control_target=self.target, token_file=self.token_file, request_timeout_s=20, lifecycle_timeout_s=310
        )


@contextmanager
def backend(custom_objects: FakeCustomObjectsApi, core: FakeCoreV1Api, token_file: Path) -> Iterator[Endpoint]:
    token_file.write_text(TOKEN)
    custom = cast(CustomObjectsClient, custom_objects)
    core_api = cast(k8s_client.CoreV1Api, core)
    inventory = SandboxInventory(namespace=NAMESPACE, custom_objects=custom, core_v1=core_api)
    with start_blocking_portal() as portal:
        resources = build_resources(
            principals=principals(AUDIENCE),
            destinations=DestinationResolver(inventory, core_api),
            caller_accounts=frozenset({MANAGER}),
        )
        with (
            portal.wrap_async_context_manager(service(resources)) as target,
            portal.wrap_async_context_manager(connection_records_api()) as api,
            portal.wrap_async_context_manager(
                control_plane(resources, principals=principals(RUNNER_AUDIENCE), api=api)
            ) as control_target,
        ):
            yield Endpoint(
                target,
                token_file,
                control_target,
                lambda sandbox_uid: portal.call(wait_connected, resources, sandbox_uid),
            )


def principals(audience: str) -> WorkloadPrincipalResolver:
    return WorkloadPrincipalResolver(
        authentication=cast(k8s_client.AuthenticationV1Api, Authentication()),
        audience=audience,
        allowed_service_account_namespaces={NAMESPACE},
    )


@asynccontextmanager
async def connection_records_api() -> AsyncIterator[k8s_client.ApiClient]:
    """An API server holding only runner connection records; the inventory stays on its fakes."""
    async with (
        fake_apiserver(dict.fromkeys(("leases", "services", "endpointslices"), NAMESPACE)) as fake,
        k8s_client.ApiClient(k8s_client.Configuration(host=f"http://127.0.0.1:{fake.port}")) as api,
    ):
        yield api
