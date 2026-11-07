"""What VM Sandboxes need on the k3s stack besides our own objects: KubeVirt and Kyverno's admission
controller, which adds the egress relay to each VM's launcher Pod (cluster/cdk8s/agentplane/launcher_relay.py).
Hosted RBE defaults to QEMU TCG; the manual KVM lane selects hardware acceleration after checking its device.
Both are the releases production runs, from their release manifests, with images from Bazel-pulled layouts.
"""

import fcntl
import json
import os
import subprocess
from collections.abc import Callable
from contextlib import ExitStack
from typing import Any

import yaml
from python.runfiles import Runfiles

from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.k3s.release_manifests import load_release_manifest

# The stack's `K3s.kubectl`: the stack calls this module while it starts.
Kubectl = Callable[..., str]

KUBEVIRT_NAMESPACE = "kubevirt"
KYVERNO_NAMESPACE = "kyverno"
_KYVERNO_ADMISSION = "kyverno-admission-controller"
READY_SECONDS = 600
_KVM_MODE_ENV = "AGENTPLANE_K3S_REQUIRE_KVM"
_KVM_GET_API_VERSION = 0xAE00
_KVM_CREATE_VM = 0xAE01
_KVM_CREATE_VCPU = 0xAE41
_KVM_API_VERSION = 12


def _kvm_requested() -> bool:
    match os.environ.get(_KVM_MODE_ENV, "0"):
        case "0":
            return False
        case "1":
            return True
        case value:
            raise ValueError(f"{_KVM_MODE_ENV} must be '0' or '1', got {value!r}")


def _assert_kvm_usable() -> None:
    """Prove this action can open KVM and create the VM/vCPU KubeVirt will need."""
    try:
        kvm_fd = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
    except OSError as error:
        raise RuntimeError("KVM mode requires a usable /dev/kvm device") from error
    with ExitStack() as descriptors:
        descriptors.callback(os.close, kvm_fd)
        try:
            api_version = fcntl.ioctl(kvm_fd, _KVM_GET_API_VERSION, 0)
        except OSError as error:
            raise RuntimeError("KVM mode could not query the /dev/kvm API version") from error
        if api_version != _KVM_API_VERSION:
            raise RuntimeError(f"/dev/kvm API version is {api_version}, expected {_KVM_API_VERSION}")
        try:
            vm_fd = fcntl.ioctl(kvm_fd, _KVM_CREATE_VM, 0)
        except OSError as error:
            raise RuntimeError("KVM mode could not create a VM") from error
        descriptors.callback(os.close, vm_fd)
        try:
            vcpu_fd = fcntl.ioctl(vm_fd, _KVM_CREATE_VCPU, 0)
        except OSError as error:
            raise RuntimeError("KVM mode could not create a vCPU") from error
        descriptors.callback(os.close, vcpu_fd)


def _manifest(variable: str) -> list[dict[str, Any]]:
    return load_release_manifest(Runfiles.CreateOrRaise().root() / os.environ[variable], variable)


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
                "developerConfiguration": {
                    "useEmulation": not _kvm_requested(),
                    "featureGates": ["EnableVirtioFsStorageVolumes"],
                }
            },
        },
    }


def baseline_objects() -> list[dict[str, Any]]:
    """The KubeVirt configuration applied after installing the operator release."""
    return [_kubevirt()]


def baseline_identity(kubectl: Kubectl) -> dict[str, str]:
    """UIDs of the controllers and KubeVirt resource that make up the VM test baseline."""
    resources = (
        ("apps/v1/deployments/kubevirt/virt-operator", "deployment/virt-operator", KUBEVIRT_NAMESPACE),
        ("apps/v1/deployments/kubevirt/virt-api", "deployment/virt-api", KUBEVIRT_NAMESPACE),
        ("apps/v1/deployments/kubevirt/virt-controller", "deployment/virt-controller", KUBEVIRT_NAMESPACE),
        ("apps/v1/daemonsets/kubevirt/virt-handler", "daemonset/virt-handler", KUBEVIRT_NAMESPACE),
        (
            "apps/v1/deployments/kyverno/kyverno-admission-controller",
            f"deployment/{_KYVERNO_ADMISSION}",
            KYVERNO_NAMESPACE,
        ),
        ("kubevirt.io/v1/kubevirts/kubevirt/kubevirt", "kubevirts.kubevirt.io/kubevirt", KUBEVIRT_NAMESPACE),
    )
    identities: dict[str, str] = {}
    for key, resource, namespace in resources:
        obj = json.loads(kubectl("get", resource, "-n", namespace, "-o", "json"))
        uid = obj["metadata"]["uid"]
        if not isinstance(uid, str) or not uid:
            raise ValueError(f"{key} has no UID")
        identities[key] = uid
    return identities


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
    if _kvm_requested():
        _assert_kvm_usable()
    return _kubevirt_operator() + _kyverno()


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
def wait_until_ready(kubectl: Kubectl, *, require_cluster_policies: bool = True) -> None:
    """Wait for KubeVirt and Kyverno; optionally wait for the Agentplane policy set too."""
    kubectl(
        "-n", KUBEVIRT_NAMESPACE, "wait", "kubevirt/kubevirt", "--for=condition=Available",
        f"--timeout={READY_SECONDS}s",
    )  # fmt: skip
    kubectl(
        "-n", KYVERNO_NAMESPACE, "wait", "--for=condition=Available", f"deployment/{_KYVERNO_ADMISSION}",
        f"--timeout={READY_SECONDS}s",
    )  # fmt: skip
    if require_cluster_policies:
        kubectl("wait", "--for=condition=Ready", "clusterpolicy", "--all", f"--timeout={READY_SECONDS}s")
