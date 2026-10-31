"""What VM Sandboxes need on the k3s stack besides our own objects: KubeVirt, emulating the guest CPU
(QEMU TCG; the RBE executor has no `/dev/kvm`), and Kyverno's admission controller, which adds the egress
relay to each VM's launcher Pod (cluster/cdk8s/agentplane/launcher_relay.py). Both are the releases
production runs, from their release manifests, with their images from Bazel-pulled layouts.
"""

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from more_itertools import one

from agentplane.system_tests.tracing import tracer
from util.bazel.runfiles import get_required_path

# The stack's `K3s.kubectl`: the stack calls this module while it starts.
Kubectl = Callable[..., str]

KUBEVIRT_NAMESPACE = "kubevirt"
KYVERNO_NAMESPACE = "kyverno"
_KYVERNO_ADMISSION = "kyverno-admission-controller"
READY_SECONDS = 600
# What virt-operator deploys, besides itself, at its own registry and version. Launcher Pods run
# virt-launcher.
_KUBEVIRT_COMPONENTS = ("virt-api", "virt-controller", "virt-handler", "virt-launcher")
_LAYOUTS = "_main/agentplane/system_tests/k3s/vm_sandboxes"


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


def releases() -> list[dict[str, Any]]:
    """KubeVirt's operator and Kyverno's admission controller, as `install` applies them."""
    return _kubevirt_operator() + _kyverno()


def images(objects: list[dict[str, Any]]) -> dict[str, Path]:
    """The `.rloc` of each image the `releases` objects run, by the reference they run it by."""
    references = {
        container["image"]
        for doc in objects
        if doc["kind"] == "Deployment"
        for container in doc["spec"]["template"]["spec"].get("initContainers", [])
        + doc["spec"]["template"]["spec"]["containers"]
    }
    registry, version = one(image for image in references if "/virt-operator:" in image).split("/virt-operator:")
    references |= {f"{registry}/{component}:{version}" for component in _KUBEVIRT_COMPONENTS}
    return {
        reference: get_required_path(
            f"{_LAYOUTS}/{reference.rsplit('/', 1)[1].split(':')[0].replace('-', '_')}_layout.rloc"
        )
        for reference in references
    }


@tracer.start_as_current_span("system_test.virtualization.install")
def install(kubectl: Kubectl, objects: list[dict[str, Any]]) -> None:
    """Start installing the `releases` objects, once their `images` are in containerd."""
    # virt-handler mounts the kubelet's Pod directory Bidirectional, which needs a shared root mount.
    subprocess.run(["mount", "--make-rshared", "/"], check=True)
    # Server-side: Kyverno's CRDs exceed the client-side apply annotation limit.
    kubectl("apply", "--server-side", "-f", "-", input_text=yaml.safe_dump_all(objects))
    kubectl("wait", "--for=condition=Established", "crd/kubevirts.kubevirt.io", "--timeout=60s")
    kubectl("apply", "-f", "-", input_text=yaml.safe_dump(_kubevirt()))


@tracer.start_as_current_span("system_test.virtualization.apis")
def wait_for_apis(kubectl: Kubectl) -> None:
    """Wait for the CRDs and Kyverno webhook before applying resources that invoke admission."""
    crds = ["crd/virtualmachines.kubevirt.io", "crd/clusterpolicies.kyverno.io"]
    # `condition=Established` fails at once on a CRD that does not exist yet.
    kubectl("wait", "--for=create", *crds, f"--timeout={READY_SECONDS}s")
    kubectl("wait", "--for=condition=Established", *crds, f"--timeout={READY_SECONDS}s")
    # A ClusterPolicy invokes Kyverno admission even before any VM is created.
    kubectl(
        "-n", KYVERNO_NAMESPACE, "wait", "--for=create", "endpoints/kyverno-svc",
        f"--timeout={READY_SECONDS}s",
    )  # fmt: skip
    kubectl(
        "-n", KYVERNO_NAMESPACE, "wait", "--for=jsonpath={.subsets[0].addresses[0].ip}", "endpoints/kyverno-svc",
        f"--timeout={READY_SECONDS}s",
    )  # fmt: skip


@tracer.start_as_current_span("system_test.virtualization.ready")
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
    """KubeVirt's and Kyverno's view of the VMs, each guest's serial console, and what Pods pulled from
    registries rather than from the imported layouts."""
    parts = [
        kubectl("get", "kubevirt,vm,vmi,clusterpolicy", "-A", "-o", "yaml", check=False),
        kubectl("get", "events", "-A", "--field-selector=reason=Pulling", check=False),
    ]
    launchers = kubectl("get", "pods", "-n", namespace, "-l", "kubevirt.io=virt-launcher", "-o", "name", check=False)
    for launcher in launchers.split():
        parts.append(f"--- {launcher} guest console")
        parts.append(kubectl("logs", "-n", namespace, launcher, "-c", "guest-console-log", check=False))
    return "\n".join(parts)
