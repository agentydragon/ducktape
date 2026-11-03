"""Infrastructure shared by the disposable and persistent Agentplane test clusters."""

from typing import Any


def kubevirt_operator(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for doc in objects:
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "virt-operator":
            doc["spec"]["replicas"] = 1
    return objects


def kubevirt(*, use_emulation: bool) -> dict[str, Any]:
    return {
        "apiVersion": "kubevirt.io/v1",
        "kind": "KubeVirt",
        "metadata": {"name": "kubevirt", "namespace": "kubevirt"},
        "spec": {
            "imagePullPolicy": "IfNotPresent",
            "infra": {"replicas": 1},
            "configuration": {
                "developerConfiguration": {
                    "useEmulation": use_emulation,
                    "featureGates": ["EnableVirtioFsStorageVolumes"],
                }
            },
        },
    }


def kyverno_admission(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the admission controller; its upstream image proxy is outside our registry mirrors."""
    objects = [
        doc
        for doc in objects
        if doc["kind"] != "Deployment" or doc["metadata"]["name"] == "kyverno-admission-controller"
    ]
    for doc in objects:
        if doc["kind"] == "Deployment":
            pod = doc["spec"]["template"]["spec"]
            for container in pod.get("initContainers", []) + pod["containers"]:
                container["image"] = container["image"].replace("reg.kyverno.io/", "ghcr.io/", 1)
    return objects
