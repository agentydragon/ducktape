"""Validate the actual image layer against the inner API and the outer network fence."""

import os
import tarfile
from pathlib import PurePosixPath
from typing import Any, cast

import pytest
import pytest_bazel
import yaml
from kubernetes_asyncio import client as k8s
from more_itertools import one
from python.runfiles import Runfiles
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.testing.kubernetes_apiserver import kubernetes_apiserver


@pytest.fixture(scope="module")
def manifests(runfiles: Runfiles) -> dict[str, list[dict[str, Any]]]:
    # Read the built layer, rather than rendering another copy disconnected from the image.
    with tarfile.open(runfiles.root() / os.environ["INFRASTRUCTURE_LAYER_RLOCATION"]) as layer:
        root = PurePosixPath("opt/agentplane-test-cluster")
        members = {PurePosixPath(member.name): member for member in layer}
        objects = {}
        for name in ("cert-manager", "trust-manager", "kubevirt-operator", "kubevirt", "kyverno", "agentplane-crds"):
            source = layer.extractfile(members[root / f"{name}.yaml"])
            assert source is not None
            objects[name] = [doc for doc in yaml.safe_load_all(source) if doc]
        assert members[root / "bootstrap"].isfile()
        return objects


def test_operator_images_are_reachable_through_the_registry_fence(manifests: dict[str, list[dict[str, Any]]]) -> None:
    # The node can only pull through Zot's configured upstreams. A new upstream needs deliberate wiring.
    allowed_registries = {"docker.io", "ghcr.io", "quay.io", "registry.k8s.io", "gcr.io"}
    for objects in manifests.values():
        for doc in objects:
            if doc["kind"] not in {"Deployment", "DaemonSet", "Job"}:
                continue
            pod = doc["spec"]["template"]["spec"]
            for container in pod.get("initContainers", []) + pod["containers"]:
                assert container["image"].split("/", 1)[0] in allowed_registries, container["image"]


async def test_kvm_required_configuration_is_accepted_by_the_pinned_api_schema(
    manifests: dict[str, list[dict[str, Any]]],
) -> None:
    definition = one(doc for doc in manifests["kubevirt-operator"] if doc["kind"] == "CustomResourceDefinition")
    configuration = one(manifests["kubevirt"])
    assert configuration["spec"]["configuration"]["developerConfiguration"]["useEmulation"] is False

    async with kubernetes_apiserver() as server:
        crds = k8s.ApiextensionsV1Api(server.api)
        await crds.create_custom_resource_definition(cast(Any, definition))
        async for attempt in AsyncRetrying(stop=stop_after_delay(30), wait=wait_fixed(0.1), reraise=True):
            with attempt:
                crd = await crds.read_custom_resource_definition(definition["metadata"]["name"])
                assert any(c.type == "Established" and c.status == "True" for c in crd.status.conditions or [])
        await k8s.CoreV1Api(server.api).create_namespace(
            k8s.V1Namespace(metadata=k8s.V1ObjectMeta(name=configuration["metadata"]["namespace"]))
        )
        created = await k8s.CustomObjectsApi(server.api).create_namespaced_custom_object(
            "kubevirt.io", "v1", configuration["metadata"]["namespace"], "kubevirts", configuration
        )
        assert created["spec"]["configuration"]["developerConfiguration"]["useEmulation"] is False


if __name__ == "__main__":
    pytest_bazel.main()
