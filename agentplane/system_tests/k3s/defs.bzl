"""What every test that starts its own k3s stack runs with: one RBE action with room for k3s."""

load("@artifact_pins//:pins.bzl", "CERT_MANAGER_V1_21_2_RELEASE_SHA256", "HELM_3_20_2_LINUX_AMD64_SHA256", "K3S_LINUX_AMD64_SHA256", "KUBEVIRT_OPERATOR_BUNDLE_SHA256", "KYVERNO_INSTALL_SHA256", "TRUST_MANAGER_CHART_V0_25_0_SHA256")
load("@image_pins//:pins.bzl", "CERT_MANAGER_CAINJECTOR_DIGEST", "CERT_MANAGER_CONTROLLER_DIGEST", "CERT_MANAGER_WEBHOOK_DIGEST", "KUBEVIRT_VIRT_API_DIGEST", "KUBEVIRT_VIRT_CONTROLLER_DIGEST", "KUBEVIRT_VIRT_HANDLER_DIGEST", "KUBEVIRT_VIRT_LAUNCHER_DIGEST", "KUBEVIRT_VIRT_OPERATOR_DIGEST", "KYVERNO_KYVERNOPRE_DIGEST", "KYVERNO_KYVERNO_DIGEST", "TRUST_MANAGER_DIGEST")

K3S_EXEC_PROPERTIES = {
    "dockerNetwork": "bridge",
    "EstimatedComputeUnits": "4",
    "EstimatedFreeDiskBytes": "50GB",
}

# BuildBuddy injects the lifecycle permission only when executing the remote action. Putting it
# in py_test.env would also enable persistence under local Bazel execution.
K3S_PERSISTENCE_PROPERTIES = {
    "test.env-overrides": "AGENTPLANE_K3S_EXECUTOR_PERSISTENCE=1",
    "test.allow-remote-snapshots": "true",
    "test.remote-snapshot-save-policy": "always",
    "test.snapshot-read-policy": "newest",
}

# Application, runner and guest images stay per-action inputs. Only pinned retained
# infrastructure selects the snapshot family; the runtime fingerprint still verifies helpers,
# CRDs, rendered releases, configuration and actual image layouts before attaching.
_COMMON_BASELINE_DIGESTS = [
    K3S_LINUX_AMD64_SHA256,
    CERT_MANAGER_V1_21_2_RELEASE_SHA256,
    TRUST_MANAGER_CHART_V0_25_0_SHA256,
    HELM_3_20_2_LINUX_AMD64_SHA256,
    CERT_MANAGER_CONTROLLER_DIGEST,
    CERT_MANAGER_WEBHOOK_DIGEST,
    CERT_MANAGER_CAINJECTOR_DIGEST,
    TRUST_MANAGER_DIGEST,
]

_VM_BASELINE_DIGESTS = [
    KUBEVIRT_OPERATOR_BUNDLE_SHA256,
    KYVERNO_INSTALL_SHA256,
    KUBEVIRT_VIRT_OPERATOR_DIGEST,
    KUBEVIRT_VIRT_API_DIGEST,
    KUBEVIRT_VIRT_CONTROLLER_DIGEST,
    KUBEVIRT_VIRT_HANDLER_DIGEST,
    KUBEVIRT_VIRT_LAUNCHER_DIGEST,
    KYVERNO_KYVERNO_DIGEST,
    KYVERNO_KYVERNOPRE_DIGEST,
]

def k3s_recycling_key(vm):
    """Select the tmpfs or disk baseline from its authoritative infrastructure pin digests."""
    return "k3s-persistence-v7-" + ("vm-" if vm else "pod-") + "-".join(
        _COMMON_BASELINE_DIGESTS + (_VM_BASELINE_DIGESTS if vm else []),
    )

# A test that runs a k3s stack in its action. Such a test is an ordinary target otherwise: PR CI
# runs it when its inputs change, and a pass caches.
K3S_TAGS = ["requires-network"]

K3S_BINARY = "@k3s_linux_amd64//file:k3s"

K3S_BINARY_RLOCATION = "$(rlocationpath " + K3S_BINARY + ")"

# KubeVirt's and Kyverno's images, by the repository name `vm_sandboxes.py` finds each layout by.
K3S_VM_IMAGES = {
    "kyverno": "@kyverno_kyverno_linux_amd64",
    "kyvernopre": "@kyverno_kyvernopre_linux_amd64",
    "virt_api": "@kubevirt_virt_api_linux_amd64",
    "virt_controller": "@kubevirt_virt_controller_linux_amd64",
    "virt_handler": "@kubevirt_virt_handler_linux_amd64",
    "virt_launcher": "@kubevirt_virt_launcher_linux_amd64",
    "virt_operator": "@kubevirt_virt_operator_linux_amd64",
}

# What a stack rendered with VM Sandboxes needs besides the stack's own: KubeVirt's and Kyverno's
# release manifests and image layouts. The suite declares its guest layouts separately.
K3S_VM_DATA = [
    "//agentplane/system_tests/k3s:vm_sandboxes/" + name + "_layout"
    for name in K3S_VM_IMAGES
] + [
    "@kubevirt_operator_bundle//file",
    "@kyverno_install//file",
]

K3S_VM_ENV = {
    "KUBEVIRT_OPERATOR_RLOCATION": "$(rlocationpath @kubevirt_operator_bundle//file)",
    "KYVERNO_INSTALL_RLOCATION": "$(rlocationpath @kyverno_install//file)",
}

# Common service images and the layouts built from them share one source of truth.
K3S_SERVICE_IMAGES = {
    "action_service": "//agentplane/action_service:service_image",
    "app": "//agentplane/app:image",
    "egress_proxy": "//agentplane/egress/cmd:image",
    "egress_sidecar": "//agentplane/egress:sidecar_image",
    "history_service": "//agentplane/history_service:service_image",
    "llm_ingress": "//agentplane/llm_ingress:image",
    "mcp_edge": "//agentplane/mcp_edge:service_image",
    "notification_service": "//agentplane/notification_service:service_image",
    "policy_controller": "//agentplane/policy_controller:image",
    "postgres": "@postgres_18_minimal_linux_amd64",
    "runner_disk": "//agentplane/env/images:disk_image",
    "runner_gateway": "//agentplane/runner_gateway:image",
    "sandbox_controller": "//agentplane/sandbox_controller:image",
}

K3S_SERVICE_LAYOUTS = ["//agentplane/system_tests/k3s:" + name + "_layout" for name in K3S_SERVICE_IMAGES]
