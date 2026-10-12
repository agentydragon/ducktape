"""Sandbox creation and incarnation-safe administrative APIs over the service boundary."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client

from agentplane.grpc_calls import RefusedError, ServiceError
from agentplane.policy_sets import POLICY_SETS_PLURAL
from agentplane.proto import runner_pb2, sandbox_service_pb2
from agentplane.proto.sandbox_service_pb2 import CreateSandboxRequest, SandboxDestination, TeardownPhase
from agentplane.sandbox_resource import SANDBOXES_PLURAL, OperatingMode
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.grpc_api import Resources
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.sandbox_service.testing.fake_inventory import (
    NAMESPACE,
    TEMPLATE,
    FakeCoreV1Api,
    FakeCustomObjectsApi,
    egress_policy,
)
from agentplane.sandbox_service.testing.grpc_service import CALLER_TOKEN, service_client
from agentplane.sandbox_service.testing.kubernetes import Cluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from util.kubernetes import CustomObjectsClient

# The generated protobuf stubs also need their library types in this target's mypy environment.
# gazelle:include_dep @pypi//protobuf

ADMIN = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="test-provisioner")
AUDIENCE = "test-provisioning"


@dataclass
class Case:
    service: Provisioning
    custom: FakeCustomObjectsApi


@pytest.fixture
def case() -> Case:
    custom = FakeCustomObjectsApi()
    inventory = SandboxInventory(
        namespace=NAMESPACE,
        custom_objects=cast(CustomObjectsClient, custom),
        core_v1=cast(k8s_client.CoreV1Api, FakeCoreV1Api()),
    )
    custom.objects[("egresspolicies", "test-basic")] = egress_policy("test-basic", [{"hosts": ["example.test"]}])
    for name in ("test-default-set", "test-chosen-set"):
        custom.objects[(POLICY_SETS_PLURAL, name)] = {"metadata": {"name": name}, "spec": {"rules": []}}
    return Case(
        Provisioning(
            inventory,
            EgressInventory(namespace=NAMESPACE, custom_objects=cast(CustomObjectsClient, custom)),
            default_policy_sets={"test-default-set"},
        ),
        custom,
    )


@asynccontextmanager
async def _client(
    provisioning: Provisioning, cluster: Cluster, token_file: Path
) -> AsyncIterator[SandboxServiceClient]:
    cluster.fake.tokens[CALLER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{ADMIN.namespace}:{ADMIN.name}",
        pod_name="test-provisioner-pod",
        pod_uid="test-provisioner-pod-uid",
        audiences=(AUDIENCE,),
    )
    resources = Resources(
        principals=cluster.build_principal_resolver(AUDIENCE),
        provisioning=provisioning,
        caller_accounts=frozenset({ADMIN}),
        admission_timeout_s=15,
        lifecycle_timeout_s=300,
    )
    async with service_client(resources, token_file) as client:
        yield client


@pytest.fixture
async def api(case: Case, cluster: Cluster, token_file: Path) -> AsyncIterator[SandboxServiceClient]:
    async with _client(case.service, cluster, token_file) as client:
        yield client


def _stored(case: Case, name: str) -> dict[str, Any]:
    return case.custom.objects[(SANDBOXES_PLURAL, name)]


async def test_create_writes_a_running_sandbox_with_its_whole_recipe(api: SandboxServiceClient, case: Case) -> None:
    spec = CreateSandboxRequest(
        name="test",
        template=TEMPLATE,
        policy_sets=["test-chosen-set"],
        bootstrap="printf ready",
        session_defaults=sandbox_service_pb2.SessionDefaults(
            harness=runner_pb2.HARNESS_CODEX, setup_script="true", instructions=""
        ),
    )
    view = await api.create(spec)
    assert _stored(case, "test")["spec"] == {
        "template": TEMPLATE,
        "running": True,
        "policySets": ["test-chosen-set", "test-default-set"],
        "sessionDefaults": {"harness": "HARNESS_CODEX", "setupScript": "true", "instructions": ""},
        "bootstrap": "printf ready",
    }
    assert view.operating_mode == OperatingMode.RUNNING
    assert view.binding.bootstrap == "printf ready"
    # An empty field set on purpose stays set, so the Session is told "no instructions".
    assert view.binding.session_defaults.HasField("instructions")
    assert not view.binding.session_defaults.HasField("cwd")
    assert await api.list_sandboxes() == [view]
    assert await api.list_templates() == [TEMPLATE]
    assert (await api.create(spec)).uid == view.uid
    with pytest.raises(ServiceError) as conflict:
        await api.create(CreateSandboxRequest(name="test", template=TEMPLATE))
    assert conflict.value.code == grpc.StatusCode.ALREADY_EXISTS


async def test_suspend_resume_and_delete_are_pinned_to_the_incarnation(api: SandboxServiceClient, case: Case) -> None:
    view = await api.create(CreateSandboxRequest(name="test", template=TEMPLATE))
    _stored(case, "test")["metadata"]["finalizers"] = ["test-controller-finalizer"]
    with pytest.raises(RefusedError):  # How the client reports FAILED_PRECONDITION: running.
        await api.delete("test")
    stale = SandboxDestination(owner=view.service_account, sandbox=view.name, sandbox_uid=str(uuid4()))
    for operation in (api.stub.SuspendSandbox, api.stub.ResumeSandbox, api.stub.DeleteSandbox):
        with pytest.raises(ServiceError) as rejected:
            await api.unary(operation, sandbox_service_pb2.SandboxRequest(destination=stale))
        assert rejected.value.code == grpc.StatusCode.NOT_FOUND
    await api.suspend("test")
    assert _stored(case, "test")["spec"]["running"] is False
    await api.resume("test")
    assert _stored(case, "test")["spec"]["running"] is True
    await api.suspend("test")
    await api.delete("test")
    await api.delete("test")
    # The controller runs it again to seal its Sessions, then deletes the rest.
    assert (await api.get("test")).teardown == TeardownPhase.TEARDOWN_PHASE_SEALING
    assert case.custom.deleted == [(SANDBOXES_PLURAL, "test")]
    with pytest.raises(RefusedError):
        await api.resume("test")


@pytest.mark.parametrize(
    "spec",
    [
        CreateSandboxRequest(template=TEMPLATE),
        CreateSandboxRequest(name="Bad_Name", template=TEMPLATE),
        CreateSandboxRequest(name="a" * 58, template=TEMPLATE),
        CreateSandboxRequest(name="test"),
        CreateSandboxRequest(name="test", template=TEMPLATE, bootstrap="x" * 65_537),
        CreateSandboxRequest(
            name="test",
            template=TEMPLATE,
            session_defaults=sandbox_service_pb2.SessionDefaults(setup_script="x" * 65_537),
        ),
        CreateSandboxRequest(
            name="test",
            template=TEMPLATE,
            session_defaults=sandbox_service_pb2.SessionDefaults(harness=runner_pb2.HARNESS_UNSPECIFIED),
        ),
        CreateSandboxRequest(name="test", template=TEMPLATE, policy_sets=["test-missing-set"]),
    ],
    ids=[
        "empty-name",
        "invalid-name",
        "long-name",
        "empty-template",
        "long-bootstrap",
        "long-setup",
        "unsupported-harness",
        "unknown-policy-set",
    ],
)
async def test_invalid_protobuf_create_does_not_mutate(
    api: SandboxServiceClient, case: Case, spec: CreateSandboxRequest
) -> None:
    before = dict(case.custom.objects)
    with pytest.raises(ServiceError) as rejected:
        await api.create(spec)
    assert rejected.value.code == grpc.StatusCode.INVALID_ARGUMENT
    assert case.custom.objects == before


async def test_authorization_precedes_creation(api: SandboxServiceClient, cluster: Cluster, case: Case) -> None:
    cluster.fake.tokens[CALLER_TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{ADMIN.namespace}:test-untrusted",
        pod_name="test-untrusted-pod",
        pod_uid="test-untrusted-pod-uid",
        audiences=(AUDIENCE,),
    )
    with pytest.raises(ServiceError) as rejected:
        await api.create(CreateSandboxRequest(name="test", template=TEMPLATE))
    assert rejected.value.code == grpc.StatusCode.PERMISSION_DENIED
    assert not await case.service.inventory.list_sandboxes()


async def test_manual_egress_grants_cross_the_service_boundary(api: SandboxServiceClient, case: Case) -> None:
    view = await api.create(CreateSandboxRequest(name="test", template=TEMPLATE))
    account = ServiceAccountRef(namespace=view.service_account.namespace, name=view.service_account.name)
    name = await api.grant_egress(view, ["test-basic"])
    assert name in {binding.name for binding in await case.service.egress.bindings_for(account)}
    await api.revoke_egress(name)
    assert name not in {binding.name for binding in await case.service.egress.bindings_for(account)}
    with pytest.raises(ServiceError) as missing:
        await api.revoke_egress(name)
    assert missing.value.code is grpc.StatusCode.NOT_FOUND
    stale = sandbox_service_pb2.Sandbox()
    stale.CopyFrom(view)
    stale.uid = str(uuid4())
    before = dict(case.custom.objects)
    with pytest.raises(ServiceError) as replaced:
        await api.grant_egress(stale, ["test-basic"])
    assert replaced.value.code is grpc.StatusCode.NOT_FOUND
    assert case.custom.objects == before


if __name__ == "__main__":
    pytest_bazel.main()
