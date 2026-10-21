"""What VM Sandboxes need on the k3s stack besides our own objects: KubeVirt, emulating the guest CPU
(QEMU TCG; the RBE executor has no `/dev/kvm`), and Kyverno's admission controller, which adds the egress
relay to each VM's launcher Pod (cluster/cdk8s/agentplane/launcher_relay.py). Both are the releases
production runs, from their release manifests; their images are pulled when the stack starts.
"""

import os
import subprocess
from collections.abc import Callable
from typing import Any

import yaml

from util.bazel.runfiles import get_required_path

# The stack's `K3s.kubectl`: the stack calls this module while it starts.
Kubectl = Callable[..., str]

KUBEVIRT_NAMESPACE = "kubevirt"
KYVERNO_NAMESPACE = "kyverno"
_KYVERNO_ADMISSION = "kyverno-admission-controller"
READY_SECONDS = 600


def _manifest(variable: str) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(get_required_path(os.environ[variable]).read_text()) if doc]


def _kubevirt_operator() -> list[dict[str, Any]]:
    objects = _manifest("KUBEVIRT_OPERATOR_RLOCATION")
    for doc in objects:
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "virt-operator":
            doc["spec"]["replicas"] = 1
    return objects


def _kubevirt() -> dict[str, Any]:
    return {
        "apiVersion": "kubevirt.io/v1",
        "kind": "KubeVirt",
        "metadata": {"name": "kubevirt", "namespace": KUBEVIRT_NAMESPACE},
        "spec": {
            "imagePullPolicy": "IfNotPresent",
            "infra": {"replicas": 1},
            "configuration": {
                # The feature gate production's runner VMs need (cluster/cdk8s/kubevirt/app.py).
                "developerConfiguration": {"useEmulation": True, "featureGates": ["EnableVirtioFsStorageVolumes"]}
            },
        },
    }


def _kyverno() -> list[dict[str, Any]]:
    """The admission controller with the CRDs and RBAC it reads; the background, cleanup and reports
    controllers serve no VM Sandbox."""
    objects = [
        doc
        for doc in _manifest("KYVERNO_INSTALL_RLOCATION")
        if doc["kind"] != "Deployment" or doc["metadata"]["name"] == _KYVERNO_ADMISSION
    ]
    for doc in objects:
        if doc["kind"] == "Deployment":
            pod = doc["spec"]["template"]["spec"]
            for container in pod.get("initContainers", []) + pod["containers"]:
                # As production pulls them (cluster/cdk8s/kyverno/app.py): reg.kyverno.io is a proxy.
                container["image"] = container["image"].replace("reg.kyverno.io/", "ghcr.io/", 1)
    return objects


def install(kubectl: Kubectl) -> None:
    """Start installing both. Their images pull while the stack's own import."""
    # virt-handler mounts the kubelet's Pod directory Bidirectional, which needs a shared root mount.
    subprocess.run(["mount", "--make-rshared", "/"], check=True)
    # Server-side: Kyverno's CRDs exceed the client-side apply annotation limit.
    kubectl("apply", "--server-side", "-f", "-", input_text=yaml.safe_dump_all(_kubevirt_operator() + _kyverno()))
    kubectl("wait", "--for=condition=Established", "crd/kubevirts.kubevirt.io", "--timeout=60s")
    kubectl("apply", "-f", "-", input_text=yaml.safe_dump(_kubevirt()))


def wait_for_apis(kubectl: Kubectl) -> None:
    """KubeVirt's operator creates the VirtualMachine API; the Sandbox Controller looks for it once, at start."""
    kubectl(
        "wait", "--for=condition=Established", "crd/virtualmachines.kubevirt.io", "crd/clusterpolicies.kyverno.io",
        f"--timeout={READY_SECONDS}s",
    )  # fmt: skip


def wait_until_ready(kubectl: Kubectl) -> None:
    """KubeVirt serves VMs, and Kyverno enforces every ClusterPolicy, the launcher relay's among them."""
    kubectl(
        "-n", KUBEVIRT_NAMESPACE, "wait", "kubevirt/kubevirt", "--for=condition=Available",
        f"--timeout={READY_SECONDS}s",
    )  # fmt: skip
    kubectl(
        "-n", KYVERNO_NAMESPACE, "wait", "--for=condition=Available", f"deployment/{_KYVERNO_ADMISSION}",
        f"--timeout={READY_SECONDS}s",
    )  # fmt: skip
    kubectl("wait", "--for=condition=Ready", "clusterpolicy", "--all", f"--timeout={READY_SECONDS}s")


def report(kubectl: Kubectl, namespace: str) -> str:
    """KubeVirt's and Kyverno's view of the VMs, and each guest's serial console."""
    parts = [kubectl("get", "kubevirt,vm,vmi,clusterpolicy", "-A", "-o", "yaml", check=False)]
    launchers = kubectl("get", "pods", "-n", namespace, "-l", "kubevirt.io=virt-launcher", "-o", "name", check=False)
    for launcher in launchers.split():
        parts.append(f"--- {launcher} guest console")
        parts.append(kubectl("logs", "-n", namespace, launcher, "-c", "guest-console-log", check=False))
    return "\n".join(parts)
