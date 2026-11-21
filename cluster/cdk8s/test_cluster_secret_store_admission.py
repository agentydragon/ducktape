"""Exercise the ESO ClusterSecretStore policy against the pinned upstream CRD and a real API server."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import cdk8s
import pytest
import pytest_bazel
import yaml
from cdk8s import App
from kubernetes_asyncio import client as k8s_client
from more_itertools import one
from python.runfiles import Runfiles
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.testing.kubernetes_apiserver import dry_run_admitted, kubernetes_apiserver, wait_until_refused
from cluster.cdk8s.external_secrets import operator

_GROUP = "external-secrets.io"
_VERSION = "v1"
_RESOURCE = "clustersecretstores"
_MESSAGE = "ClusterSecretStore must name at least one allowed namespace"


def _store(kind: str, name: str, **spec: object) -> dict[str, Any]:
    return {
        "apiVersion": f"{_GROUP}/{_VERSION}",
        "kind": kind,
        "metadata": {"name": name},
        "spec": {"provider": {"fake": {"data": [{"key": "example", "value": "test"}]}}, **spec},
    }


@pytest.fixture
def eso_store_crds() -> list[dict[str, Any]]:
    bundle = Path(str(Runfiles.CreateOrRaise().Rlocation(os.environ["ESO_CRDS"])))
    return [
        document
        for document in yaml.safe_load_all(bundle.read_text())
        if document and document["metadata"]["name"] in {f"{_RESOURCE}.{_GROUP}", f"secretstores.{_GROUP}"}
    ]


async def test_cluster_secret_store_admission(scratch_app: App, eso_store_crds: list[dict[str, Any]]) -> None:
    docs = cast(list[dict[str, Any]], cdk8s.Testing.synth(operator.chart(scratch_app)))
    policy = one(document for document in docs if document["kind"] == "ValidatingAdmissionPolicy")
    binding = one(document for document in docs if document["kind"] == "ValidatingAdmissionPolicyBinding")
    allowed = [{"namespaces": ["consumer"]}]
    malformed: tuple[dict[str, object], ...] = (
        {},
        {"conditions": None},
        {"conditions": []},
        {"conditions": [{}]},
        {"conditions": [{"namespaces": []}]},
        {"conditions": [{"namespaces": None}]},
        {"conditions": [{"namespaceSelector": {}}]},
        {"conditions": [{"namespaceSelector": {"matchLabels": {"test": "allowed"}}}]},
        {"conditions": [{"namespaceRegexes": ["^consumer$"]}]},
    )
    async with kubernetes_apiserver() as server:
        assert len(eso_store_crds) == 2
        await server.install_crds(*eso_store_crds)

        stores = k8s_client.CustomObjectsApi(server.api)
        # Seed a pre-policy violation to exercise the grandfathering path.
        existing = _store("ClusterSecretStore", "existing")
        await stores.create_cluster_custom_object(_GROUP, _VERSION, _RESOURCE, existing)
        await stores.create_cluster_custom_object(
            _GROUP, _VERSION, _RESOURCE, _store("ClusterSecretStore", "fenced", conditions=allowed)
        )
        await server.install_admission_policy(policy, binding, namespaces=["consumer"])

        def create(**spec: object):
            return stores.create_cluster_custom_object(
                _GROUP, _VERSION, _RESOURCE, _store("ClusterSecretStore", "candidate", **spec), dry_run="All"
            )

        await wait_until_refused(lambda: dry_run_admitted(create()))
        for invalid in malformed:
            with pytest.raises(k8s_client.ApiException, match=_MESSAGE) as error:
                await create(**invalid)
            assert error.value.status == 422
        await create(conditions=allowed)
        await create(conditions=[{}, *allowed])

        # An existing violation may receive updates, but cannot recur once repaired.
        await stores.patch_cluster_custom_object(
            _GROUP,
            _VERSION,
            _RESOURCE,
            "existing",
            [{"op": "add", "path": "/metadata/labels", "value": {"test": "updated"}}],
            dry_run="All",
        )
        await stores.patch_cluster_custom_object(
            _GROUP, _VERSION, _RESOURCE, "existing", [{"op": "add", "path": "/spec/conditions", "value": allowed}]
        )
        for name in ("existing", "fenced"):
            with pytest.raises(k8s_client.ApiException, match=_MESSAGE):
                await stores.patch_cluster_custom_object(
                    _GROUP,
                    _VERSION,
                    _RESOURCE,
                    name,
                    [{"op": "replace", "path": "/spec/conditions", "value": []}],
                    dry_run="All",
                )

        await stores.patch_cluster_custom_object_status(
            _GROUP, _VERSION, _RESOURCE, "fenced", [{"op": "add", "path": "/status", "value": {}}], dry_run="All"
        )
        await stores.delete_cluster_custom_object(_GROUP, _VERSION, _RESOURCE, "fenced", dry_run="All")
        await stores.create_namespaced_custom_object(
            _GROUP, _VERSION, "consumer", "secretstores", _store("SecretStore", "namespaced"), dry_run="All"
        )

        # Grant the node group authorization only in this isolated API so admission decides.
        rbac = k8s_client.RbacAuthorizationV1Api(server.api)
        await rbac.create_cluster_role_binding(
            k8s_client.V1ClusterRoleBinding(
                metadata=k8s_client.V1ObjectMeta(name="admission-test-nodes"),
                role_ref=k8s_client.V1RoleRef(
                    api_group="rbac.authorization.k8s.io", kind="ClusterRole", name="cluster-admin"
                ),
                subjects=[
                    k8s_client.RbacV1Subject(api_group="rbac.authorization.k8s.io", kind="Group", name="system:nodes")
                ],
            )
        )
        async with server.impersonating("system:node:test") as node:
            node.set_default_header("Impersonate-Group", "system:nodes")
            async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.1), reraise=True):
                with attempt:
                    await k8s_client.CustomObjectsApi(node).create_cluster_custom_object(
                        _GROUP, _VERSION, _RESOURCE, _store("ClusterSecretStore", "node-request"), dry_run="All"
                    )


if __name__ == "__main__":
    pytest_bazel.main()
