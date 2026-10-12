"""Headless provisioning, restart recovery, and incarnation-safe administrative APIs."""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from more_itertools import one

from agentplane import sandbox_resource
from agentplane.crd_group import GROUP
from agentplane.grpc_calls import RefusedError, ServiceError
from agentplane.proto import runner_pb2, sandbox_service_pb2
from agentplane.proto.sandbox_service_pb2 import CreateSandboxRequest, SandboxDestination, TeardownPhase
from agentplane.sandbox_service.action_policy import ActionPolicyBindings
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.grpc_api import Resources
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.kubernetes_bindings import KubernetesBindings
from agentplane.sandbox_service.kubernetes_grants import RoleBindingGrant, RoleRef
from agentplane.sandbox_service.kubernetes_views import SANDBOX_BINDING_ANNOTATION
from agentplane.sandbox_service.provisioning import OwnSandboxes, Provisioning
from agentplane.sandbox_service.testing.fake_inventory import (
    NAMESPACE,
    RUNNER_ENDPOINTS,
    TEMPLATE,
    FakeCoreV1Api,
    FakeCustomObjectsApi,
    action_policy_set,
    egress_policy,
    pod,
)
from agentplane.sandbox_service.testing.fake_rbac import FakeRbac
from agentplane.sandbox_service.testing.grpc_service import CALLER_TOKEN, service_client
from agentplane.sandbox_service.testing.kubernetes import Cluster
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from util.kubernetes import CustomObjectsClient

# The generated protobuf stubs also need their library types in this target's mypy environment.
# gazelle:include_dep @pypi//protobuf

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
    custom.add_runner_endpoint_policies()
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
            RUNNER_ENDPOINTS,
            own_sandboxes=None,
        ),
        custom,
        core,
        rbac,
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


async def test_headless_create_list_and_uid_pinned_lifecycle(api: SandboxServiceClient, case: Case) -> None:
    view = await api.create(
        CreateSandboxRequest(
            name="test",
            template=TEMPLATE,
            action_policy_sets=["test-actions"],
            kubernetes_grants=["test-read"],
            bootstrap="printf ready",
            session_defaults=sandbox_service_pb2.SessionDefaults(model="test-model", instructions=""),
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
        "session_defaults": {"model": "test-model", "instructions": ""},
        "bootstrap": "printf ready",
    }
    assert view.initializing
    assert view.operating_mode == "Suspended"
    assert not case.core.service_accounts
    with pytest.raises(RefusedError):
        await api.resume(view.name)
    await case.service.reconcile_once()
    view = await api.get(view.name)
    assert not view.initializing
    assert view.operating_mode == "Running"
    assert view.kubernetes_grants_ready
    assert len(case.rbac.bindings) == 1
    assert await case.service.inventory.pending_grants(view.name) is None
    assert await api.list_sandboxes() == [view]
    assert await api.list_templates() == [TEMPLATE]
    with pytest.raises(RefusedError):
        await api.delete(view.name)
    stale = SandboxDestination(owner=view.service_account, sandbox=view.name, sandbox_uid=str(uuid4()))
    for operation in (api.stub.SuspendSandbox, api.stub.ResumeSandbox, api.stub.DeleteSandbox):
        with pytest.raises(ServiceError) as rejected:
            await api.unary(operation, sandbox_service_pb2.SandboxRequest(destination=stale))
        assert rejected.value.code == grpc.StatusCode.NOT_FOUND
    await api.suspend(view.name)
    await api.resume(view.name)
    await api.suspend(view.name)
    await api.delete(view.name)


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
        # The runner's endpoints are the service's to grant, never the request's to pick.
        CreateSandboxRequest(name="test", template=TEMPLATE, egress_policies=[RUNNER_ENDPOINTS.ingress.egress_policy]),
        CreateSandboxRequest(name="test", template=TEMPLATE, egress_policies=[RUNNER_ENDPOINTS.control.egress_policy]),
    ],
    ids=[
        "empty-name",
        "invalid-name",
        "long-name",
        "empty-template",
        "long-bootstrap",
        "long-setup",
        "unsupported-harness",
        "picks-ingress-policy",
        "picks-control-policy",
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
    await case.service.create(
        CreateSandboxRequest(
            name="test", template=TEMPLATE, action_policy_sets=["test-actions"], kubernetes_grants=["test-read"]
        ),
        caller=ADMIN,
    )
    await case.service.reconcile_once()
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
    await case.service.create(
        CreateSandboxRequest(name="test", template=TEMPLATE, action_policy_sets=["test-actions"]), caller=ADMIN
    )
    await case.service.reconcile_once()
    (view,) = await case.service.inventory.list_sandboxes()
    binding = next(value for (kind, _), value in case.custom.objects.items() if kind == "egressbindings")
    binding["spec"]["policies"] = ["test-foreign"]
    case.custom.fail_plural = None
    await case.service.reconcile_once()
    assert binding["spec"]["policies"] == ["test-foreign"]
    assert await case.service.inventory.pending_grants(view.name) is not None


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
    assert not case.core.service_accounts
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


async def test_create_retry_finishes_initialization_after_lost_grants(case: Case) -> None:
    spec = CreateSandboxRequest(name="retry", template=TEMPLATE, action_policy_sets=["test-actions"])
    case.custom.fail_plural = "actionpolicybindings"
    await case.service.create(spec, caller=ADMIN)
    await case.service.reconcile_once()
    view = await case.service.inventory.get("retry")
    assert view.operating_mode == "Suspended"
    assert "retry" in case.core.service_accounts
    case.custom.fail_plural = None
    await case.service.reconcile_once()
    completed = await case.service.create(spec, caller=ADMIN)
    assert completed.uid == view.uid
    assert completed.operating_mode == "Running"
    assert await case.service.inventory.pending_grants("retry") is None
    assert (await case.service.create(spec, caller=ADMIN)).uid == view.uid


async def test_create_retry_after_lost_service_account_reply(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    original = case.core.create_namespaced_service_account
    called = False

    async def committed_then_lost(namespace: str, body: k8s_client.V1ServiceAccount) -> k8s_client.V1ServiceAccount:
        nonlocal called
        created = await original(namespace, body)
        if not called:
            called = True
            raise k8s_client.ApiException(status=503)
        return created

    monkeypatch.setattr(case.core, "create_namespaced_service_account", committed_then_lost)
    spec = CreateSandboxRequest(name="sa-lost", template=TEMPLATE)
    view = await case.service.create(spec, caller=ADMIN)
    assert view.initializing
    await case.service.reconcile_once()
    view = await case.service.inventory.get(view.name)
    assert view.operating_mode == "Running"
    assert list(case.core.service_accounts) == ["sa-lost"]
    assert (await case.service.create(spec, caller=ADMIN)).uid == view.uid


async def test_reconcile_after_final_resume_reply_is_lost(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    original = case.custom.patch_namespaced_custom_object
    lost = False

    async def patched_then_lost(
        group: str, version: str, namespace: str, plural: str, name: str, body: object, *, _content_type: str
    ) -> object:
        nonlocal lost
        result = await original(group, version, namespace, plural, name, body, _content_type=_content_type)
        if not lost and isinstance(body, dict) and body.get("spec", {}).get("operatingMode") == "Running":
            lost = True
            raise k8s_client.ApiException(status=503)
        return result

    monkeypatch.setattr(case.custom, "patch_namespaced_custom_object", patched_then_lost)
    spec = CreateSandboxRequest(name="last-patch", template=TEMPLATE)
    view = await case.service.create(spec, caller=ADMIN)
    assert view.initializing
    await case.service.reconcile_once()  # The patch commits but its response is lost.
    await case.service.reconcile_once()  # A restart sees the already-running CR.
    current = await case.service.inventory.get(view.name)
    assert current.operating_mode == "Running"
    assert not current.initializing


def _runner_args(case: Case, name: str) -> list[str]:
    containers = case.custom.objects[("sandboxes", name)]["spec"]["podTemplate"]["spec"]["containers"]
    args: list[str] = one(container for container in containers if container["name"] == "runner")["args"]
    return args


def _granted(case: Case) -> list[str]:
    binding = one(value for (kind, _), value in case.custom.objects.items() if kind == "egressbindings")
    policies: list[str] = binding["spec"]["policies"]
    return policies


async def test_create_points_the_runner_at_its_endpoints_and_grants_their_egress(case: Case) -> None:
    await case.service.create(CreateSandboxRequest(name="test", template=TEMPLATE), caller=ADMIN)
    assert _runner_args(case, "test") == [
        "--control-target",
        RUNNER_ENDPOINTS.control.target,
        "--control-bearer",
        RUNNER_ENDPOINTS.control.bearer,
        "--ingress-target",
        RUNNER_ENDPOINTS.ingress.target,
        "--ingress-bearer",
        RUNNER_ENDPOINTS.ingress.bearer,
    ]
    await case.service.reconcile_once()
    assert _granted(case) == [
        RUNNER_ENDPOINTS.control.egress_policy,
        RUNNER_ENDPOINTS.ingress.egress_policy,
        "test-basic",
    ]


async def test_endpoint_egress_cannot_be_granted_afterwards(api: SandboxServiceClient, case: Case) -> None:
    view = await api.create(CreateSandboxRequest(name="test", template=TEMPLATE))
    before = dict(case.custom.objects)
    with pytest.raises(ServiceError) as rejected:
        await api.grant_egress(view, [RUNNER_ENDPOINTS.ingress.egress_policy])
    assert rejected.value.code == grpc.StatusCode.INVALID_ARGUMENT
    assert case.custom.objects == before


@pytest.mark.parametrize("flag", ["--control-target", "--ingress-target"])
async def test_a_template_that_names_an_endpoint_is_refused(api: SandboxServiceClient, case: Case, flag: str) -> None:
    template = deepcopy(case.custom.objects[("sandboxtemplates", TEMPLATE)])
    template["spec"]["podTemplate"]["spec"]["containers"][0]["args"] = [flag, "test-elsewhere.test:9000"]
    case.custom.objects[("sandboxtemplates", TEMPLATE)] = template
    with pytest.raises(RefusedError):  # How the client reports FAILED_PRECONDITION.
        await api.create(CreateSandboxRequest(name="test", template=TEMPLATE))
    assert ("sandboxes", "test") not in case.custom.objects


# Agentplane's own Sandbox kind: the deployment's `own_sandboxes` setting.

OWN_STORE = f"{sandbox_resource.SANDBOXES_PLURAL}.{GROUP}"


@pytest.fixture
async def own_api(case: Case, cluster: Cluster, token_file: Path) -> AsyncIterator[SandboxServiceClient]:
    for name in ("test-default-set", "test-chosen-set"):
        case.custom.objects[("policysets", name)] = {"metadata": {"name": name}, "spec": {"rules": []}}
    case.service = replace(case.service, own_sandboxes=OwnSandboxes(default_policy_sets={"test-default-set"}))
    async with _client(case.service, cluster, token_file) as client:
        yield client


def _own(case: Case, name: str) -> dict[str, Any]:
    return case.custom.objects[(OWN_STORE, name)]


async def test_create_writes_our_kind_running_with_its_whole_recipe(own_api: SandboxServiceClient, case: Case) -> None:
    spec = CreateSandboxRequest(
        name="test",
        template=TEMPLATE,
        policy_sets=["test-chosen-set"],
        bootstrap="printf ready",
        session_defaults=sandbox_service_pb2.SessionDefaults(harness=runner_pb2.HARNESS_CODEX, setup_script="true"),
    )
    view = await own_api.create(spec)
    assert _own(case, "test")["spec"] == {
        "template": TEMPLATE,
        "running": True,
        "policySets": ["test-chosen-set", "test-default-set"],
        "sessionDefaults": {"harness": "HARNESS_CODEX", "setupScript": "true"},
        "bootstrap": "printf ready",
    }
    assert view.operating_mode == "Running"
    assert view.binding.bootstrap == "printf ready"
    assert ("sandboxes", "test") not in case.custom.objects
    # Nothing to provision here: the controller makes the ServiceAccount and the PolicyBinding.
    assert not case.core.service_accounts
    assert (await own_api.create(spec)).uid == view.uid
    with pytest.raises(ServiceError) as conflict:
        await own_api.create(CreateSandboxRequest(name="test", template=TEMPLATE))
    assert conflict.value.code == grpc.StatusCode.ALREADY_EXISTS


@pytest.mark.parametrize(
    "spec",
    [
        CreateSandboxRequest(name="test", template=TEMPLATE, policy_sets=["test-missing-set"]),
        CreateSandboxRequest(name="test", template=TEMPLATE, egress_policies=["test-basic"]),
        CreateSandboxRequest(name="test", template=TEMPLATE, action_policy_sets=["test-actions"]),
        CreateSandboxRequest(name="test", template=TEMPLATE, kubernetes_grants=["test-read"]),
    ],
    ids=["unknown-policy-set", "egress-policy", "action-policy-set", "kubernetes-grant"],
)
async def test_our_kind_takes_known_policy_sets_alone(
    own_api: SandboxServiceClient, case: Case, spec: CreateSandboxRequest
) -> None:
    before = dict(case.custom.objects)
    with pytest.raises(ServiceError) as rejected:
        await own_api.create(spec)
    assert rejected.value.code == grpc.StatusCode.INVALID_ARGUMENT
    assert case.custom.objects == before


async def test_policy_sets_are_refused_for_agent_sandboxes(api: SandboxServiceClient, case: Case) -> None:
    with pytest.raises(ServiceError) as rejected:
        await api.create(CreateSandboxRequest(name="test", template=TEMPLATE, policy_sets=["test-chosen-set"]))
    assert rejected.value.code == grpc.StatusCode.INVALID_ARGUMENT
    assert ("sandboxes", "test") not in case.custom.objects


async def test_a_name_held_by_the_other_kind_is_refused(
    api: SandboxServiceClient, own_api: SandboxServiceClient, case: Case
) -> None:
    await api.create(CreateSandboxRequest(name="test", template=TEMPLATE))
    with pytest.raises(ServiceError) as conflict:
        await own_api.create(CreateSandboxRequest(name="test", template=TEMPLATE, policy_sets=["test-chosen-set"]))
    assert conflict.value.code == grpc.StatusCode.ALREADY_EXISTS
    assert (OWN_STORE, "test") not in case.custom.objects


async def test_our_kind_suspends_resumes_and_deletes_by_its_spec(own_api: SandboxServiceClient, case: Case) -> None:
    view = await own_api.create(CreateSandboxRequest(name="test", template=TEMPLATE))
    _own(case, "test")["metadata"]["finalizers"] = ["test-controller-finalizer"]
    with pytest.raises(RefusedError):  # How the client reports FAILED_PRECONDITION: running.
        await own_api.delete("test")
    stale = SandboxDestination(owner=view.service_account, sandbox=view.name, sandbox_uid=str(uuid4()))
    for operation in (own_api.stub.SuspendSandbox, own_api.stub.ResumeSandbox, own_api.stub.DeleteSandbox):
        with pytest.raises(ServiceError) as rejected:
            await own_api.unary(operation, sandbox_service_pb2.SandboxRequest(destination=stale))
        assert rejected.value.code == grpc.StatusCode.NOT_FOUND
    await own_api.suspend("test")
    assert _own(case, "test")["spec"]["running"] is False
    await own_api.resume("test")
    assert _own(case, "test")["spec"]["running"] is True
    await own_api.suspend("test")
    await own_api.delete("test")
    await own_api.delete("test")
    # The controller runs it again to seal its Sessions, then deletes the rest.
    assert (await own_api.get("test")).teardown == TeardownPhase.TEARDOWN_PHASE_SEALING
    assert case.custom.deleted == [(OWN_STORE, "test")]
    with pytest.raises(RefusedError):
        await own_api.resume("test")


async def test_provisioning_leaves_our_kind_to_its_controller(
    own_api: SandboxServiceClient, case: Case, caplog: pytest.LogCaptureFixture
) -> None:
    await own_api.create(CreateSandboxRequest(name="test", template=TEMPLATE))
    before = deepcopy(_own(case, "test"))
    with caplog.at_level(logging.ERROR):
        await case.service.reconcile_once()
    assert not caplog.records
    assert _own(case, "test") == before


if __name__ == "__main__":
    pytest_bazel.main()
