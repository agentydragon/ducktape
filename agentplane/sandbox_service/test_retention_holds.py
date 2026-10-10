"""Retention holds over the authenticated gRPC boundary: deletion waits for every hold, and a hold
racing deletion either refuses it or is refused, never both lost."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import cast

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client

from agentplane.runner.errors import RunnerError
from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.action_policy import ActionPolicyBindings
from agentplane.sandbox_service.client import SandboxServiceClient, ServiceError
from agentplane.sandbox_service.destinations import DestinationResolver
from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.grpc_api import Resources
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.kubernetes_bindings import KubernetesBindings
from agentplane.sandbox_service.kubernetes_views import RETENTION_HOLDS_ANNOTATION
from agentplane.sandbox_service.models import SandboxNotFoundError
from agentplane.sandbox_service.protocol_pb2 import Hold
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.sandbox_service.testing.fake_inventory import NAMESPACE, FakeCoreV1Api, FakeCustomObjectsApi, sandbox
from agentplane.sandbox_service.testing.fake_rbac import FakeRbac
from agentplane.sandbox_service.testing.grpc_service import service_client
from agentplane.sandbox_service.testing.kubernetes import Cluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.agent_sandbox import SANDBOXES_PLURAL
from util.kubernetes import CustomObjectsClient

# gazelle:include_dep @pypi//protobuf

TOKEN = "test-holder-token"
AUDIENCE = "test-retention-holds"
HOLDER_ACCOUNT = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="test-holder")
NAME = "test-held-sandbox"
SESSION = "test-held-session"


class RacingObjects(FakeCustomObjectsApi):
    """Runs `concurrently` once, right before the next write reaches the API server, as a request
    another replica committed between this one's read and its write would."""

    concurrently: Callable[[], Awaitable[object]] | None = None

    async def _race(self) -> None:
        if (other := self.concurrently) is not None:
            self.concurrently = None
            await other()

    async def patch_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str, body: object, *, _content_type: str
    ) -> object:
        await self._race()
        return await super().patch_namespaced_custom_object(
            group, version, namespace, plural, name, body, _content_type=_content_type
        )

    async def delete_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str, *, body: k8s_client.V1DeleteOptions
    ) -> object:
        await self._race()
        return await super().delete_namespaced_custom_object(group, version, namespace, plural, name, body=body)


@pytest.fixture
def custom_objects() -> RacingObjects:
    objects = RacingObjects()
    objects.objects[(SANDBOXES_PLURAL, NAME)] = sandbox(NAME, operating_mode="Suspended")
    return objects


@pytest.fixture
def destination(custom_objects: RacingObjects) -> protocol_pb2.SessionDestination:
    return protocol_pb2.SessionDestination(
        sandbox=protocol_pb2.SandboxDestination(
            owner=protocol_pb2.ServiceAccount(namespace=NAMESPACE, name=NAME),
            sandbox=NAME,
            sandbox_uid=custom_objects.objects[(SANDBOXES_PLURAL, NAME)]["metadata"]["uid"],
        ),
        session_id=SESSION,
    )


@pytest.fixture
async def api(
    cluster: Cluster, inventory: SandboxInventory, custom_objects: RacingObjects, core_v1: FakeCoreV1Api, tmp_path: Path
) -> AsyncIterator[SandboxServiceClient]:
    cluster.fake.tokens[TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{HOLDER_ACCOUNT.namespace}:{HOLDER_ACCOUNT.name}",
        pod_name="test-holder-pod",
        pod_uid="test-holder-pod-uid",
        audiences=(AUDIENCE,),
    )
    custom = cast(CustomObjectsClient, custom_objects)
    resources = Resources(
        runner_admission_ack_timeout_s=1,
        principals=WorkloadPrincipalResolver(
            authentication=k8s_client.AuthenticationV1Api(cluster.api),
            audience=AUDIENCE,
            allowed_service_account_namespaces={SANDBOX_NAMESPACE},
        ),
        destinations=DestinationResolver(inventory, cast(k8s_client.CoreV1Api, core_v1), 7000),
        caller_accounts=frozenset({HOLDER_ACCOUNT}),
        platform_instructions="",
        provisioning=Provisioning(
            inventory,
            EgressInventory(namespace=NAMESPACE, custom_objects=custom, default_policies=[]),
            ActionPolicyBindings(namespace=NAMESPACE, custom_objects=custom),
            {},
            KubernetesBindings(inventory, cast(k8s_client.RbacAuthorizationV1Api, FakeRbac())),
        ),
    )
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    async with service_client(resources, token_file) as client:
        yield client


async def place(
    api: SandboxServiceClient, destination: protocol_pb2.SessionDestination, holder: str = "history"
) -> Hold:
    return await api.unary(api.stub.PlaceHold, protocol_pb2.HoldRequest(destination=destination, holder=holder))


async def confirm(
    api: SandboxServiceClient, destination: protocol_pb2.SessionDestination, cursor: int, holder: str = "history"
) -> Hold:
    return await api.unary(
        api.stub.ConfirmHold,
        protocol_pb2.ConfirmHoldRequest(destination=destination, holder=holder, through_cursor=cursor),
    )


async def release(
    api: SandboxServiceClient, destination: protocol_pb2.SessionDestination, holder: str = "history"
) -> None:
    await api.unary(api.stub.ReleaseHold, protocol_pb2.HoldRequest(destination=destination, holder=holder))


async def holds(api: SandboxServiceClient, destination: protocol_pb2.SessionDestination) -> list[Hold]:
    listed = await api.unary(api.stub.ListHolds, protocol_pb2.SandboxRequest(destination=destination.sandbox))
    return list(listed.holds)


async def test_sandbox_without_holds_deletes(api: SandboxServiceClient, custom_objects: RacingObjects) -> None:
    await api.delete(NAME)
    assert (SANDBOXES_PLURAL, NAME) not in custom_objects.objects


async def test_unconfirmed_hold_keeps_the_sandbox_and_is_listed_until_released(
    api: SandboxServiceClient, destination: protocol_pb2.SessionDestination, custom_objects: RacingObjects
) -> None:
    await place(api, destination)
    await confirm(api, destination, 7)
    # Holds never expire: the deletion stays refused however often it is retried.
    for _ in range(2):
        with pytest.raises(RunnerError):
            await api.delete(NAME)
    assert custom_objects.deleted == []
    assert await holds(api, destination) == [Hold(session_id=SESSION, holder="history", confirmed_through=7)]

    await release(api, destination)
    await api.delete(NAME)
    assert (SANDBOXES_PLURAL, NAME) not in custom_objects.objects


async def test_deletion_waits_for_every_holder_to_confirm_through_the_seal(
    api: SandboxServiceClient, destination: protocol_pb2.SessionDestination, custom_objects: RacingObjects
) -> None:
    await place(api, destination, "history")
    await place(api, destination, "archiver")
    # Written as the runner's teardown seal will record it, which nothing does yet.
    annotations = custom_objects.objects[(SANDBOXES_PLURAL, NAME)]["metadata"]["annotations"]
    stored = json.loads(annotations[RETENTION_HOLDS_ANNOTATION])
    stored["sessions"][SESSION]["seal_cursor"] = 10
    annotations[RETENTION_HOLDS_ANNOTATION] = json.dumps(stored)

    await confirm(api, destination, 10, "history")
    assert await confirm(api, destination, 9, "archiver") == Hold(
        session_id=SESSION, holder="archiver", confirmed_through=9, seal_cursor=10
    )
    with pytest.raises(RunnerError):
        await api.delete(NAME)
    assert await holds(api, destination) == [
        Hold(session_id=SESSION, holder="archiver", confirmed_through=9, seal_cursor=10),
        Hold(session_id=SESSION, holder="history", confirmed_through=10, seal_cursor=10),
    ]

    await confirm(api, destination, 12, "archiver")
    await api.delete(NAME)
    assert (SANDBOXES_PLURAL, NAME) not in custom_objects.objects


async def test_hold_is_idempotent_per_holder_and_confirmation_never_regresses(
    api: SandboxServiceClient, destination: protocol_pb2.SessionDestination
) -> None:
    await place(api, destination)
    await confirm(api, destination, 5)
    assert await place(api, destination) == Hold(session_id=SESSION, holder="history", confirmed_through=5)
    assert await confirm(api, destination, 3) == Hold(session_id=SESSION, holder="history", confirmed_through=5)
    with pytest.raises(ServiceError) as absent:
        await confirm(api, destination, 5, "never-placed")
    assert absent.value.code == grpc.StatusCode.NOT_FOUND
    stale = protocol_pb2.SessionDestination()
    stale.CopyFrom(destination)
    stale.sandbox.sandbox_uid = "test-previous-incarnation"
    with pytest.raises(ServiceError) as wrong_incarnation:
        await place(api, stale)
    assert wrong_incarnation.value.code == grpc.StatusCode.NOT_FOUND
    await release(api, destination)
    await release(api, destination)
    assert not await holds(api, destination)


async def test_hold_placed_while_deletion_is_in_flight_refuses_it(
    api: SandboxServiceClient,
    destination: protocol_pb2.SessionDestination,
    inventory: SandboxInventory,
    custom_objects: RacingObjects,
) -> None:
    custom_objects.concurrently = lambda: inventory.place_hold(
        NAME, uid=destination.sandbox.sandbox_uid, session_id=SESSION, holder="history"
    )
    with pytest.raises(RunnerError):
        await api.delete(NAME)
    assert custom_objects.deleted == []
    assert await holds(api, destination) == [Hold(session_id=SESSION, holder="history")]


@pytest.mark.parametrize("finalizers", [[], ["test.agentplane/cleanup"]])
async def test_hold_racing_a_committed_deletion_is_refused(
    api: SandboxServiceClient,
    destination: protocol_pb2.SessionDestination,
    inventory: SandboxInventory,
    custom_objects: RacingObjects,
    finalizers: list[str],
) -> None:
    custom_objects.objects[(SANDBOXES_PLURAL, NAME)]["metadata"]["finalizers"] = finalizers
    custom_objects.concurrently = lambda: inventory.delete(NAME, uid=destination.sandbox.sandbox_uid)
    with pytest.raises(ServiceError) as refused:
        await place(api, destination)
    assert refused.value.code == grpc.StatusCode.NOT_FOUND
    assert custom_objects.deleted == [(SANDBOXES_PLURAL, NAME)]
    if finalizers:
        assert (await api.get(NAME)).deleting
        assert not await holds(api, destination)
    else:
        with pytest.raises(SandboxNotFoundError):
            await api.get(NAME)


if __name__ == "__main__":
    pytest_bazel.main()
