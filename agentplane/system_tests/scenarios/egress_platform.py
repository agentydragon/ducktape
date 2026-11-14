"""Kubernetes and proxy observations shared by App-backed and native Egress scenarios."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.crd_group import GROUP, VERSION
from agentplane.egress.api.admin.egress_admin_pb2 import Decision
from agentplane.environments.v1 import environment_service_pb2
from agentplane.sandbox_resource import SANDBOX_API_VERSION, SANDBOX_KIND
from agentplane.system_tests.driver import egress_decisions
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.tracing import tracer
from cluster.cdk8s.agentplane.namespaces import kubernetes_accounts_namespace

# Generated protocol stubs import protobuf types that mypy resolves through direct deps.
# gazelle:include_dep @pypi//protobuf

KUBERNETES_HOST = "kubernetes.default.svc.cluster.local"
UNLISTED_HOST = "example.com"
DECISION_SECONDS = 30.0
BINDING_SECONDS = 60.0


async def decision_for(
    cluster: Cluster,
    sandbox: environment_service_pb2.EnvironmentDescriptor,
    host: str,
    *,
    reader_service_account: str,
    after: datetime | None = None,
) -> Decision:
    """The newest decision for this live Sandbox Identity about `host`, after `after`."""
    with tracer.start_as_current_span(
        "system_test.egress.proxy_decision", record_exception=False, set_status_on_exception=False
    ):
        assert sandbox.identity_name
        async with egress_decisions.watch_client(cluster, service_account=reader_service_account) as (stub, caller):
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(DECISION_SECONDS), wait=wait_fixed(1), reraise=True
            ):
                with attempt:
                    decisions = await egress_decisions.snapshot_identity(stub, caller, sandbox.identity_name)
                    for decision in reversed(decisions):
                        if decision.host == host and (after is None or decision.at.ToDatetime(tzinfo=UTC) > after):
                            return decision
                    raise AssertionError(
                        f"no decision about {host} after {after}; the history contains {[d.host for d in decisions]}"
                    )
    raise AssertionError("unreachable: reraise=True either returns a decision or raises")


async def kubectl_create_binding(
    cluster: Cluster, sandbox: environment_service_pb2.EnvironmentDescriptor, policy_set: str
) -> str:
    """Bind `policy_set` to the Sandbox Identity, owned by the Sandbox; return the binding name."""
    body = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "PolicyBinding",
        "metadata": {
            "generateName": f"{sandbox.target.name}-",
            "ownerReferences": [
                {
                    "apiVersion": SANDBOX_API_VERSION,
                    "kind": SANDBOX_KIND,
                    "name": sandbox.target.name,
                    "uid": str(sandbox.target.uid),
                }
            ],
        },
        "spec": {"subject": {"identity": {"name": sandbox.identity_name}}, "policySets": [policy_set]},
    }
    with tracer.start_as_current_span(
        "system_test.egress.policy_binding.apply", record_exception=False, set_status_on_exception=False
    ):
        return await cluster.kubectl("create", "-f", "-", "-o", "jsonpath={.metadata.name}", body=body)


@asynccontextmanager
async def create_kubernetes_probe(
    cluster: Cluster, view: environment_service_pb2.EnvironmentDescriptor
) -> AsyncIterator[tuple[str, str, str, str, str]]:
    """Create a private ConfigMap and grant its exact get permission through the Sandbox Identity."""
    assert view.identity_name
    suffix = secrets.token_hex(4)
    name = f"{view.target.name}-kubernetes-probe"
    policy_set = f"k8s-probe-{suffix}"
    account_namespace = kubernetes_accounts_namespace(cluster.namespace)
    account = f"probe-{suffix}"
    value = secrets.token_urlsafe(32)
    owner_references = [
        {"apiVersion": SANDBOX_API_VERSION, "kind": SANDBOX_KIND, "name": view.target.name, "uid": str(view.target.uid)}
    ]
    resource_metadata: dict[str, object] = {"namespace": cluster.namespace, "ownerReferences": owner_references}
    named_metadata = {**resource_metadata, "name": name}
    await cluster.kubectl(
        "create",
        "-n",
        account_namespace,
        "-f",
        "-",
        body={"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": account}},
    )
    try:
        await cluster.kubectl(
            "create",
            "-f",
            "-",
            body={"apiVersion": "v1", "kind": "ConfigMap", "metadata": named_metadata, "data": {"value": value}},
        )
        await cluster.kubectl(
            "create",
            "-f",
            "-",
            body={
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": named_metadata,
                "rules": [{"apiGroups": [""], "resources": ["configmaps"], "resourceNames": [name], "verbs": ["get"]}],
            },
        )
        await cluster.kubectl(
            "create",
            "-f",
            "-",
            body={
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": named_metadata,
                "subjects": [{"kind": "ServiceAccount", "name": account, "namespace": account_namespace}],
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
            },
        )
        await cluster.kubectl(
            "create",
            "-f",
            "-",
            body={
                "apiVersion": f"{GROUP}/{VERSION}",
                "kind": "EgressCredential",
                "metadata": {"name": policy_set, "ownerReferences": owner_references},
                "spec": {
                    "description": f"Read ConfigMap {cluster.namespace}/{name} as {account_namespace}/{account}.",
                    "source": {"kubernetesServiceAccount": {"namespace": account_namespace, "name": account}},
                    "targets": [{"header": "Authorization", "method": "schemeToken", "scheme": "Bearer"}],
                },
            },
        )
        await cluster.kubectl(
            "create",
            "-f",
            "-",
            body={
                "apiVersion": f"{GROUP}/{VERSION}",
                "kind": "PolicySet",
                "metadata": {"name": policy_set, "ownerReferences": owner_references},
                "spec": {
                    "rules": [
                        {
                            "type": "egress",
                            "hosts": [KUBERNETES_HOST],
                            "clusterInternal": True,
                            "credentialRef": {"name": policy_set},
                        }
                    ]
                },
            },
        )
        binding = await kubectl_create_binding(cluster, view, policy_set)
        await cluster.kubectl("wait", "--for=condition=EgressReady", f"policybinding/{binding}", "--timeout=90s")
        yield name, cluster.namespace, value, account, policy_set
    finally:
        await cluster.kubectl(
            "delete", "serviceaccount", account, "-n", account_namespace, "--ignore-not-found", "--wait=true"
        )
