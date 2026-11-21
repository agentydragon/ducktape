"""Exercise the generated provider CRDs against Kubernetes admission."""

from typing import Any

import pytest
import pytest_bazel
import yaml
from kubernetes_asyncio import client as k8s
from kubernetes_asyncio.client.exceptions import ApiException
from python.runfiles import Runfiles

from agentplane.testing.kubernetes_apiserver import kubernetes_apiserver


async def test_generated_crds_and_api_key_capabilities() -> None:
    directory = Runfiles.CreateOrRaise().root() / "ducktape/cluster/providers/buildbuddy/apis/v1alpha1/crds"
    crd_paths = sorted(directory.glob("*.yaml"))
    assert crd_paths

    async with kubernetes_apiserver() as server:
        await server.install_crds(*(yaml.safe_load(path.read_text()) for path in crd_paths))
        await k8s.CoreV1Api(server.api).create_namespace(k8s.V1Namespace(metadata=k8s.V1ObjectMeta(name="test")))
        api = k8s.CustomObjectsApi(server.api)

        def key(name: str, capabilities: list[str]) -> dict[str, Any]:
            return {
                "apiVersion": "buildbuddy.m.allegedly.works/v1alpha1",
                "kind": "APIKey",
                "metadata": {"name": name},
                "spec": {"forProvider": {"groupID": "test", "label": name, "capabilities": capabilities}},
            }

        await api.create_namespaced_custom_object(
            "buildbuddy.m.allegedly.works", "v1alpha1", "test", "apikeys", key("valid", ["CACHE_WRITE", "CAS_WRITE"])
        )
        with pytest.raises(ApiException) as rejected:
            await api.create_namespaced_custom_object(
                "buildbuddy.m.allegedly.works",
                "v1alpha1",
                "test",
                "apikeys",
                key("duplicate", ["CAS_WRITE", "CAS_WRITE"]),
            )
        assert rejected.value.status == 422


if __name__ == "__main__":
    pytest_bazel.main()
