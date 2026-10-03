"""Headless provisioning, restart recovery, and incarnation-safe administrative APIs."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client

from agentplane.runner import protocol_pb2 as runner_pb2
from agentplane.runner.errors import RunnerError
from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.action_policy import ActionPolicyBindings
from agentplane.sandbox_service.client import SandboxServiceClient, ServiceError
from agentplane.sandbox_service.destinations import DestinationResolver
from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.grpc_api import Resources
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.kubernetes_bindings import KubernetesBindings
from agentplane.sandbox_service.kubernetes_grants import RoleBindingGrant, RoleRef
from agentplane.sandbox_service.kubernetes_views import SANDBOX_BINDING_ANNOTATION
from agentplane.sandbox_service.protocol_pb2 import CreateSandboxRequest, SandboxDestination
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.sandbox_service.testing.fake_inventory import (
    NAMESPACE,
    TEMPLATE,
    FakeCoreV1Api,
    FakeCustomObjectsApi,
    action_policy_set,
    egress_policy,
    pod,
)
from agentplane.sandbox_service.testing.fake_rbac import FakeRbac
from agentplane.sandbox_service.testing.grpc_service import service
from agentplane.sandbox_service.testing.kubernetes import Cluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.kubernetes import CustomObjectsClient

# The generated protobuf stubs also need their library types in this target's mypy environment.
# gazelle:include_dep @pypi//protobuf

TOKEN = "test-provisioner-token"
ADMIN = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="test-provisioner")
AUDIENCE = "test-provisioning"


class FaultyObjects(FakeCustomObjectsApi):
    fail_plural: str | None = None

    async def create_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        if plural == self.fail_plural:
            raise k8s_client.ApiException(status=503)
        return await super().create_namespaced_custom_object(group, version, namespace, plural, body)


@dataclass
class Case:
    service: Provisioning
    custom: FaultyObjects
    core: FakeCoreV1Api
    rbac: FakeRbac


@pytest.fixture
def case() -> Case:
    custom, core, rbac = FaultyObjects(), FakeCoreV1Api(), FakeRbac()
    inventory = SandboxInventory(
        namespace=NAMESPACE, custom_objects=cast(CustomObjectsClient, custom), core_v1=cast(k8s_client.CoreV1Api, core)
    )
    custom.objects[("egresspolicies", "test-basic")] = egress_policy("test-basic", [{"hosts": ["example.test"]}])
    custom.objects[("actionpolicysets", "test-actions")] = action_policy_set("test-actions")
    return Case(
        Provisioning(
            inventory,
            EgressInventory(
                namespace=NAMESPACE, custom_objects=cast(CustomObjectsClient, custom), default_policies=["test-basic"]
            ),
            ActionPolicyBindings(namespace=NAMESPACE, custom_objects=cast(CustomObjectsClient, custom)),
            {
                "test-read": RoleBindingGrant(
                    kind="RoleBinding", namespace=NAMESPACE, role_ref=RoleRef(kind="Role", name="test-reader")
                )
            },
            KubernetesBindings(inventory, cast(k8s_client.RbacAuthorizationV1Api, rbac)),
        ),
        custom,
        core,
        rbac,
    )


@pytest.fixture
async def api(case: Case, cluster: Cluster, tmp_path: Path) -> AsyncIterator[SandboxServiceClient]:
    cluster.fake.tokens[TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{ADMIN.namespace}:{ADMIN.name}",
        pod_name="test-provisioner-pod",
        pod_uid="test-provisioner-pod-uid",
        audiences=(AUDIENCE,),
    )
    resources = Resources(
        principals=WorkloadPrincipalResolver(
            authentication=k8s_client.AuthenticationV1Api(cluster.api),
            audience=AUDIENCE,
            allowed_service_account_namespaces={SANDBOX_NAMESPACE},
        ),
        destinations=DestinationResolver(case.service.inventory, cast(k8s_client.CoreV1Api, case.core), 7000),
        caller_accounts=frozenset({ADMIN}),
        platform_instructions="",
        provisioning=case.service,
    )
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    async with service(resources) as target:
        client = SandboxServiceClient(target, namespace=NAMESPACE, token_file=token_file)
        try:
            yield client
        finally:
            await client.close()


async def test_headless_create_list_and_uid_pinned_lifecycle(api: SandboxServiceClient, case: Case) -> None:
    view = await api.create(
        CreateSandboxRequest(
            slug="test",
            template=TEMPLATE,
            action_policy_sets=["test-actions"],
            kubernetes_grants=["test-read"],
            bootstrap="printf ready",
            session_defaults=protocol_pb2.SessionDefaults(model="test-model", instructions=""),
        )
    )
    assert view.HasField("binding")
    assert view.binding.bootstrap == "printf ready"
    assert view.binding.HasField("session_defaults")
    assert view.binding.session_defaults.HasField("instructions")
    assert view.binding.session_defaults.instructions == ""
    assert not view.binding.session_defaults.HasField("cwd")
    stored = case.custom.objects[("sandboxes", view.name)]["metadata"]["annotations"]
    assert json.loads(stored[SANDBOX_BINDING_ANNOTATION]) == {
        "thread_defaults": {"model": "test-model", "instructions": ""},
        "bootstrap": "printf ready",
    }
    assert view.kubernetes_grants_ready
    assert len(case.rbac.bindings) == 1
    assert await case.service.inventory.pending_grants(view.name) is None
    assert await api.list_sandboxes() == [view]
    assert await api.list_templates() == [TEMPLATE]
    with pytest.raises(RunnerError):
        await api.delete(view.name)
    stale = SandboxDestination(owner=view.service_account, sandbox=view.name, sandbox_uid=str(uuid4()))
    for operation in (api.stub.SuspendSandbox, api.stub.ResumeSandbox, api.stub.DeleteSandbox):
        with pytest.raises(ServiceError) as rejected:
            await api.unary(operation, protocol_pb2.SandboxRequest(destination=stale))
        assert rejected.value.code == grpc.StatusCode.NOT_FOUND
    await api.suspend(view.name)
    await api.resume(view.name)
    await api.suspend(view.name)
    await api.delete(view.name)


@pytest.mark.parametrize(
    "spec",
    [
        CreateSandboxRequest(template=TEMPLATE),
        CreateSandboxRequest(slug="Bad_Name", template=TEMPLATE),
        CreateSandboxRequest(slug="a" * 58, template=TEMPLATE),
        CreateSandboxRequest(slug="test"),
        CreateSandboxRequest(slug="test", template=TEMPLATE, bootstrap="x" * 65_537),
        CreateSandboxRequest(
            slug="test", template=TEMPLATE, session_defaults=protocol_pb2.SessionDefaults(setup_script="x" * 65_537)
        ),
        CreateSandboxRequest(
            slug="test",
            template=TEMPLATE,
            session_defaults=protocol_pb2.SessionDefaults(harness=runner_pb2.HARNESS_UNSPECIFIED),
        ),
    ],
    ids=[
        "empty-slug",
        "invalid-slug",
        "long-slug",
        "empty-template",
        "long-bootstrap",
        "long-setup",
        "unsupported-harness",
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
    assert not case.core.service_accounts
    assert not case.rbac.bindings


async def test_partial_create_recovers_from_kubernetes_state_without_app(case: Case) -> None:
    case.custom.fail_plural = "actionpolicybindings"
    with pytest.raises(k8s_client.ApiException):
        await case.service.create(
            CreateSandboxRequest(
                slug="test", template=TEMPLATE, action_policy_sets=["test-actions"], kubernetes_grants=["test-read"]
            )
        )
    (view,) = await case.service.inventory.list_sandboxes()
    case.core.pods[view.name] = pod(view.name, phase="Running", ready=True, ip="10.0.0.1")
    assert (await case.service.inventory.get(view.name)).launch_grants_pending
    assert await case.service.inventory.pending_grants(view.name) is not None
    case.custom.fail_plural = None
    # A fresh service with a changed catalog must use the recorded concrete grant, not current UI defaults.
    restarted = replace(case.service, grants={})
    await restarted.reconcile_once()
    await restarted.reconcile_once()
    assert await restarted.inventory.pending_grants(view.name) is None
    recovered = await restarted.inventory.get(view.name)
    assert not recovered.launch_grants_pending
    assert recovered.kubernetes_grants_ready
    assert len([key for key in case.custom.objects if key[0] == "egressbindings"]) == 1
    assert len([key for key in case.custom.objects if key[0] == "actionpolicybindings"]) == 1
    assert len(case.rbac.bindings) == 1
    assert next(iter(case.rbac.bindings.values())).role_ref.name == "test-reader"


async def test_foreign_binding_is_not_overwritten_and_provisioning_stays_pending(case: Case) -> None:
    case.custom.fail_plural = "actionpolicybindings"
    with pytest.raises(k8s_client.ApiException):
        await case.service.create(
            CreateSandboxRequest(slug="test", template=TEMPLATE, action_policy_sets=["test-actions"])
        )
    (view,) = await case.service.inventory.list_sandboxes()
    binding = next(value for (kind, _), value in case.custom.objects.items() if kind == "egressbindings")
    binding["spec"]["policies"] = ["test-foreign"]
    case.custom.fail_plural = None
    await case.service.reconcile_once()
    assert binding["spec"]["policies"] == ["test-foreign"]
    assert await case.service.inventory.pending_grants(view.name) is not None


async def test_authorization_precedes_creation(api: SandboxServiceClient, cluster: Cluster, case: Case) -> None:
    cluster.fake.tokens[TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{ADMIN.namespace}:test-untrusted",
        pod_name="test-untrusted-pod",
        pod_uid="test-untrusted-pod-uid",
        audiences=(AUDIENCE,),
    )
    with pytest.raises(ServiceError) as rejected:
        await api.create(CreateSandboxRequest(slug="test", template=TEMPLATE))
    assert rejected.value.code == grpc.StatusCode.PERMISSION_DENIED
    assert not case.core.service_accounts
    assert not await case.service.inventory.list_sandboxes()


async def test_manual_egress_grants_cross_the_service_boundary(api: SandboxServiceClient, case: Case) -> None:
    view = await api.create(CreateSandboxRequest(slug="test", template=TEMPLATE))
    name = await api.grant_egress(view, ["test-basic"])
    assert name in {
        binding.name
        for binding in await case.service.egress.bindings_for(
            ServiceAccountRef(namespace=view.service_account.namespace, name=view.service_account.name)
        )
    }
    await api.revoke_egress(name)
    assert name not in {
        binding.name
        for binding in await case.service.egress.bindings_for(
            ServiceAccountRef(namespace=view.service_account.namespace, name=view.service_account.name)
        )
    }
    with pytest.raises(ServiceError) as missing:
        await api.revoke_egress(name)
    assert missing.value.code is grpc.StatusCode.NOT_FOUND
    stale = protocol_pb2.Sandbox()
    stale.CopyFrom(view)
    stale.uid = str(uuid4())
    before = dict(case.custom.objects)
    with pytest.raises(ServiceError) as replaced:
        await api.grant_egress(stale, ["test-basic"])
    assert replaced.value.code is grpc.StatusCode.NOT_FOUND
    assert case.custom.objects == before


if __name__ == "__main__":
    pytest_bazel.main()
