"""Headless provisioning, restart recovery, and incarnation-safe administrative APIs."""

import json
from collections.abc import AsyncIterator
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
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.runner import protocol_pb2 as runner_pb2
from agentplane.runner.errors import RunnerError
from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.action_policy import ActionPolicyBindings
from agentplane.sandbox_service.channel_control import ChannelControl
from agentplane.sandbox_service.channel_delivery import ChannelDelivery
from agentplane.sandbox_service.client import SandboxServiceClient, ServiceError
from agentplane.sandbox_service.commands.connections import ConnectionStore
from agentplane.sandbox_service.commands.dispatch import DispatchStore
from agentplane.sandbox_service.commands.store import SubmissionStore
from agentplane.sandbox_service.connection_records import ConnectionLeases
from agentplane.sandbox_service.destinations import DestinationResolver
from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.grpc_api import Resources
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.kubernetes_bindings import KubernetesBindings
from agentplane.sandbox_service.kubernetes_grants import RoleBindingGrant, RoleRef
from agentplane.sandbox_service.kubernetes_views import SANDBOX_BINDING_ANNOTATION
from agentplane.sandbox_service.protocol_pb2 import CreateSandboxRequest, SandboxDestination
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.sandbox_service.runner_endpoints import RunnerEndpoint, RunnerEndpoints
from agentplane.sandbox_service.session_history.session_changes import SessionChanges
from agentplane.sandbox_service.session_registry import SessionRegistry
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
# gazelle:include_dep //agentplane/sandbox_service/commands:conftest

pytest_plugins = ("agentplane.sandbox_service.commands.conftest",)

TOKEN = "test-provisioner-token"
ADMIN = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="test-provisioner")
AUDIENCE = "test-provisioning"
CONTROL = RunnerEndpoint(target="test-hub.test:8082", bearer="test-control-bearer", egress_policy="test-control")
HISTORY = RunnerEndpoint(target="test-history.test:8083", bearer="test-history-bearer", egress_policy="test-history")


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
    for endpoint in (CONTROL, HISTORY):
        custom.objects[("egresspolicies", endpoint.egress_policy)] = egress_policy(
            endpoint.egress_policy, [{"hosts": [endpoint.target.partition(":")[0]]}]
        )
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
            RunnerEndpoints(control=CONTROL, ingress=HISTORY),
        ),
        custom,
        core,
        rbac,
    )


@pytest.fixture
async def api(case: Case, cluster: Cluster, engine: AsyncEngine, tmp_path: Path) -> AsyncIterator[SandboxServiceClient]:
    cluster.fake.tokens[TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{ADMIN.namespace}:{ADMIN.name}",
        pod_name="test-provisioner-pod",
        pod_uid="test-provisioner-pod-uid",
        audiences=(AUDIENCE,),
    )
    resources = Resources(
        runner_admission_ack_timeout_s=1,
        principals=WorkloadPrincipalResolver(
            authentication=k8s_client.AuthenticationV1Api(cluster.api),
            audience=AUDIENCE,
            allowed_service_account_namespaces={SANDBOX_NAMESPACE},
        ),
        destinations=DestinationResolver(case.service.inventory, cast(k8s_client.CoreV1Api, case.core), 7000),
        caller_accounts=frozenset({ADMIN}),
        platform_instructions="",
        provisioning=case.service,
        sessions=SessionRegistry(engine),
        submissions=SubmissionStore(engine),
        delivery=ChannelDelivery(
            engine.url,
            dispatches=DispatchStore(engine),
            connections=ConnectionStore(engine),
            leases=ConnectionLeases(),
            liveness_s=1,
        ),
        control=ChannelControl(ConnectionLeases()),
        session_changes=SessionChanges(engine.url),
    )
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    async with service(resources) as target:
        client = SandboxServiceClient(
            target,
            namespace=NAMESPACE,
            token_file=token_file,
            command_admission_timeout_s=20,
            request_timeout_s=20,
            lifecycle_timeout_s=310,
            follow_timeout_s=960,
        )
        try:
            yield client
        finally:
            await client.close()


async def test_headless_create_list_and_uid_pinned_lifecycle(api: SandboxServiceClient, case: Case) -> None:
    view = await api.create(
        CreateSandboxRequest(
            name="test",
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
        "session_defaults": {"model": "test-model", "instructions": ""},
        "bootstrap": "printf ready",
    }
    assert view.initializing
    assert view.operating_mode == "Suspended"
    assert not case.core.service_accounts
    with pytest.raises(RunnerError):
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
        CreateSandboxRequest(name="Bad_Name", template=TEMPLATE),
        CreateSandboxRequest(name="a" * 58, template=TEMPLATE),
        CreateSandboxRequest(name="test"),
        CreateSandboxRequest(name="test", template=TEMPLATE, bootstrap="x" * 65_537),
        CreateSandboxRequest(
            name="test", template=TEMPLATE, session_defaults=protocol_pb2.SessionDefaults(setup_script="x" * 65_537)
        ),
        CreateSandboxRequest(
            name="test",
            template=TEMPLATE,
            session_defaults=protocol_pb2.SessionDefaults(harness=runner_pb2.HARNESS_UNSPECIFIED),
        ),
    ],
    ids=[
        "empty-name",
        "invalid-name",
        "long-name",
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
    cluster.fake.tokens[TOKEN] = TokenVerdict(
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
        "--channel-target",
        CONTROL.target,
        "--channel-bearer",
        CONTROL.bearer,
        "--ingress-target",
        HISTORY.target,
        "--ingress-bearer",
        HISTORY.bearer,
    ]
    await case.service.reconcile_once()
    assert _granted(case) == [CONTROL.egress_policy, HISTORY.egress_policy, "test-basic"]


async def test_an_unset_ingress_starts_the_runner_without_one(case: Case) -> None:
    unconfigured = replace(case.service, endpoints=RunnerEndpoints(control=CONTROL, ingress=None))
    await unconfigured.create(CreateSandboxRequest(name="test", template=TEMPLATE), caller=ADMIN)
    assert _runner_args(case, "test") == ["--channel-target", CONTROL.target, "--channel-bearer", CONTROL.bearer]
    await unconfigured.reconcile_once()
    assert _granted(case) == [CONTROL.egress_policy, "test-basic"]


@pytest.mark.parametrize(
    "spec",
    [
        CreateSandboxRequest(name="test", template=TEMPLATE, egress_policies=[HISTORY.egress_policy]),
        CreateSandboxRequest(name="test", template=TEMPLATE, egress_policies=[CONTROL.egress_policy]),
    ],
    ids=["picks-ingress-policy", "picks-control-policy"],
)
async def test_a_request_cannot_pick_an_endpoint_egress_policy(
    api: SandboxServiceClient, case: Case, spec: CreateSandboxRequest
) -> None:
    before = dict(case.custom.objects)
    with pytest.raises(ServiceError) as rejected:
        await api.create(spec)
    assert rejected.value.code == grpc.StatusCode.INVALID_ARGUMENT
    assert case.custom.objects == before


async def test_endpoint_egress_cannot_be_granted_afterwards(api: SandboxServiceClient, case: Case) -> None:
    view = await api.create(CreateSandboxRequest(name="test", template=TEMPLATE))
    before = dict(case.custom.objects)
    with pytest.raises(ServiceError) as rejected:
        await api.grant_egress(view, [HISTORY.egress_policy])
    assert rejected.value.code == grpc.StatusCode.INVALID_ARGUMENT
    assert case.custom.objects == before


@pytest.mark.parametrize("flag", ["--channel-target", "--ingress-target"])
async def test_a_template_that_names_an_endpoint_is_refused(api: SandboxServiceClient, case: Case, flag: str) -> None:
    template = deepcopy(case.custom.objects[("sandboxtemplates", TEMPLATE)])
    template["spec"]["podTemplate"]["spec"]["containers"][0]["args"] = [flag, "test-elsewhere.test:9000"]
    case.custom.objects[("sandboxtemplates", TEMPLATE)] = template
    with pytest.raises(RunnerError):  # How the client reports FAILED_PRECONDITION.
        await api.create(CreateSandboxRequest(name="test", template=TEMPLATE))
    assert ("sandboxes", "test") not in case.custom.objects


if __name__ == "__main__":
    pytest_bazel.main()
