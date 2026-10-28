"""What every test that starts its own k3s stack runs with: one RBE action with room for k3s."""

# No extra pod runner-recycling-key: resource properties already distinguish k3s snapshots.
# 2026-10-23 A/B (2 warm runs/arm): both retained the same 121-blob tmpfs cache;
# median imports were 7.50s without a key vs 7.75s with one. No demonstrated benefit.
K3S_EXEC_PROPERTIES = {
    "dockerNetwork": "bridge",
    "EstimatedComputeUnits": "4",
    "EstimatedFreeDiskBytes": "50GB",
}

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
# release manifests and image layouts, and the test guest's images.
K3S_VM_DATA = [
    "//agentplane/system_tests/k3s:vm_sandboxes/" + name + "_layout"
    for name in K3S_VM_IMAGES
] + [
    "//agentplane/system_tests/k3s/guest:vm_guest_boot_layout",
    "@kubevirt_operator_bundle//file",
    "@kyverno_install//file",
]

K3S_VM_ENV = {
    "KUBEVIRT_OPERATOR_RLOCATION": "$(rlocationpath @kubevirt_operator_bundle//file)",
    "KYVERNO_INSTALL_RLOCATION": "$(rlocationpath @kyverno_install//file)",
}
