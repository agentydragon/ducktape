"""Shared image inputs and execution properties for k3s system tests."""

load("@artifact_pins//:pins.bzl", "CERT_MANAGER_V1_21_2_RELEASE_SHA256", "HELM_3_20_2_LINUX_AMD64_SHA256", "K3S_LINUX_AMD64_SHA256", "KUBEVIRT_OPERATOR_BUNDLE_SHA256", "KYVERNO_INSTALL_SHA256", "TRUST_MANAGER_CHART_V0_25_0_SHA256")
load("@aspect_bazel_lib//lib:paths.bzl", "to_rlocation_path")
load("@image_pins//:pins.bzl", "CERT_MANAGER_CAINJECTOR_DIGEST", "CERT_MANAGER_CAINJECTOR_REFERENCE", "CERT_MANAGER_CONTROLLER_DIGEST", "CERT_MANAGER_CONTROLLER_REFERENCE", "CERT_MANAGER_WEBHOOK_DIGEST", "CERT_MANAGER_WEBHOOK_REFERENCE", "GENERIC_DEVICE_PLUGIN_REFERENCE", "KUBEVIRT_VIRT_API_DIGEST", "KUBEVIRT_VIRT_API_REFERENCE", "KUBEVIRT_VIRT_CONTROLLER_DIGEST", "KUBEVIRT_VIRT_CONTROLLER_REFERENCE", "KUBEVIRT_VIRT_HANDLER_DIGEST", "KUBEVIRT_VIRT_HANDLER_REFERENCE", "KUBEVIRT_VIRT_LAUNCHER_DIGEST", "KUBEVIRT_VIRT_LAUNCHER_REFERENCE", "KUBEVIRT_VIRT_OPERATOR_DIGEST", "KUBEVIRT_VIRT_OPERATOR_REFERENCE", "KYVERNO_KYVERNOPRE_DIGEST", "KYVERNO_KYVERNOPRE_REFERENCE", "KYVERNO_KYVERNO_DIGEST", "KYVERNO_KYVERNO_REFERENCE", "TRUST_MANAGER_DIGEST", "TRUST_MANAGER_REFERENCE")

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
    # Leave remote-snapshot-save-policy unset: forced saves overload BuildBuddy's snapshot infrastructure.
    "test.snapshot-read-policy": "newest",
}

# Where a KVM test runs. Its size is not defaulted here: each KVM suite declares test.EstimatedCPU,
# test.EstimatedMemory and test.EstimatedFreeDiskBytes in its own exec_properties, beside the test it describes.
KVM_EXEC_PROPERTIES = {
    "test.Pool": "kvm-vm",
    "test.init-dockerd": "false",
    "test.recycle-runner": "false",
    "test.use-self-hosted-executors": "true",
    "test.workload-isolation-type": "docker",
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
    return "k3s-persistence-v7-" + "-".join(
        _COMMON_BASELINE_DIGESTS + (_VM_BASELINE_DIGESTS if vm else []),
    )

# A test that runs a k3s stack in its action. Such a test is an ordinary target otherwise: PR CI
# runs it when its inputs change, and a pass caches.
K3S_TAGS = ["requires-network"]

K3S_BINARY = "@k3s_linux_amd64//file:k3s"

K3S_BINARY_RLOCATION = "$(rlocationpath " + K3S_BINARY + ")"

# Each entry is an OCI image target mapped to the reference used when importing it into k3s.
# Keep shared image facts here; each BUILD target selects the image groups it needs.
# Fixture and guest references stay under baseline.FIXTURE_IMAGE_REPOSITORY: a stack removes
# such images that its manifest does not declare.
BACKEND_SERVICE_IMAGES = {
    "//agentplane/action_service:service_image": "git.allegedly.works/ducktape-ci/agentplane-action-service:unset",
    "//agentplane/egress/cmd:image": "git.allegedly.works/ducktape-ci/agentplane-egress-proxy:unset",
    "//agentplane/egress:sidecar_image": "git.allegedly.works/ducktape-ci/agentplane-egress-sidecar:unset",
    "//agentplane/history_service:service_image": "git.allegedly.works/ducktape-ci/agentplane-history-service:unset",
    "//agentplane/llm_ingress:image": "git.allegedly.works/ducktape-ci/agentplane-llm-ingress:unset",
    "//agentplane/notification_service:service_image": "git.allegedly.works/ducktape-ci/agentplane-notification-service:unset",
    "//agentplane/policy_controller:image": "git.allegedly.works/ducktape-ci/agentplane-policy-controller:unset",
    "//agentplane/session_dispatcher:image": "git.allegedly.works/ducktape-ci/agentplane-sessions:unset",
    "//agentplane/environments:image": "git.allegedly.works/ducktape-ci/agentplane-environments:unset",
    "@postgres_18_minimal_linux_amd64": "git.allegedly.works/ducktape-ci/agentplane-postgres:unset",
}
APP_IMAGES = {
    "//agentplane/app:image": "git.allegedly.works/ducktape-ci/agentplane-app:unset",
}
HARNESSES_IMAGES = {
    "//agentplane/system_tests/deployment:environment_tools_harnesses_image": "git.allegedly.works/ducktape-ci/environment-tools:unset",
}
CENTRAL_IMAGES = {
    "//agentplane/sampler/cmd:service_image": "git.allegedly.works/ducktape-ci/agentplane-central-sampler:unset",
}
MCP_FIXTURE_IMAGES = {
    "@dex_linux_amd64": "git.allegedly.works/ducktape-ci/agentplane-dex:unset",
    "@mcp_everything_linux_amd64": "git.allegedly.works/ducktape-ci/agentplane-mcp-everything:unset",
    "//agentplane/action_service/testing:oauth_mcp_server_image": "git.allegedly.works/ducktape-ci/agentplane-oauth-fixture:unset",
}
PKI_IMAGES = {
    "@cert_manager_controller_linux_amd64": CERT_MANAGER_CONTROLLER_REFERENCE,
    "@cert_manager_webhook_linux_amd64": CERT_MANAGER_WEBHOOK_REFERENCE,
    "@cert_manager_cainjector_linux_amd64": CERT_MANAGER_CAINJECTOR_REFERENCE,
    "@trust_manager_linux_amd64": TRUST_MANAGER_REFERENCE,
}
VM_IMAGES = {
    "@kubevirt_virt_api_linux_amd64": KUBEVIRT_VIRT_API_REFERENCE,
    "@kubevirt_virt_controller_linux_amd64": KUBEVIRT_VIRT_CONTROLLER_REFERENCE,
    "@kubevirt_virt_handler_linux_amd64": KUBEVIRT_VIRT_HANDLER_REFERENCE,
    "@kubevirt_virt_launcher_linux_amd64": KUBEVIRT_VIRT_LAUNCHER_REFERENCE,
    "@kubevirt_virt_operator_linux_amd64": KUBEVIRT_VIRT_OPERATOR_REFERENCE,
    "@kyverno_kyverno_linux_amd64": KYVERNO_KYVERNO_REFERENCE,
    "@kyverno_kyvernopre_linux_amd64": KYVERNO_KYVERNOPRE_REFERENCE,
}
VM_GUEST_BOOT_IMAGE = {
    "//agentplane/system_tests/vm/guest:boot_image": "git.allegedly.works/ducktape-ci/agentplane-vm-guest-boot:unset",
}
VM_GUEST_ROOT_IMAGE = {
    "//agentplane/system_tests/vm/guest:root_image": "git.allegedly.works/ducktape-ci/agentplane-vm-guest-root:unset",
}
VM_GUEST_ROOT_PROFILE_IMAGE = {
    "//agentplane/system_tests/vm/guest:root_profile_image": "git.allegedly.works/ducktape-ci/agentplane-vm-guest-root-profile:unset",
}
RUNNER_DISK_IMAGE = {
    "//agentplane/env/images:disk_image": "git.allegedly.works/ducktape-ci/agentplane-runner-disk:unset",
}
DELETION_TEST_DISK_IMAGE = {
    "//agentplane/system_tests/deployment:deletion_test_disk_image": "git.allegedly.works/ducktape-ci/agentplane-runner-disk:unset",
}
VM_RUNNER_DISK_IMAGE = {
    "//agentplane/system_tests/vm:test_runner_disk_image": "git.allegedly.works/ducktape-ci/agentplane-runner-disk:unset",
}

FIRECRACKER_GUEST_IMAGES = {
    "//agentplane/firecracker:image": "git.allegedly.works/ducktape-ci/agentplane-firecracker-launcher:unset",
    "//agentplane/system_tests/firecracker:boot_image": "git.allegedly.works/ducktape-ci/agentplane-environment-firecracker:unset",
}

MCP_EDGE_IMAGES = {
    "//agentplane/mcp_edge:service_image": "git.allegedly.works/ducktape-ci/agentplane-mcp-edge:unset",
}
GENERIC_DEVICE_PLUGIN_IMAGE = {
    "@generic_device_plugin_linux_amd64": GENERIC_DEVICE_PLUGIN_REFERENCE,
}

def _image_manifest_impl(ctx):
    groups = {}
    runfiles = ctx.runfiles()
    for name in ["fixture", "pki", "vm", "guest"]:
        references = {}
        for image, reference in getattr(ctx.attr, name).items():
            files = image[DefaultInfo].files.to_list()
            if len(files) != 1:
                fail("expected one OCI layout from %s, got %s" % (image.label, files))
            if reference in references:
                fail("duplicate image reference in %s: %s" % (name, reference))
            references[reference] = to_rlocation_path(ctx, files[0])
            runfiles = runfiles.merge(ctx.runfiles(transitive_files = image[DefaultInfo].files))
            runfiles = runfiles.merge(image[DefaultInfo].default_runfiles)
        groups[name] = references
    out = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(out, json.encode(groups))
    return [DefaultInfo(files = depset([out]), runfiles = runfiles.merge(ctx.runfiles([out])))]

image_manifest = rule(
    implementation = _image_manifest_impl,
    attrs = {name: attr.label_keyed_string_dict(allow_files = True) for name in ["fixture", "pki", "vm", "guest"]},
    doc = "Write grouped image references and OCI layout runfiles paths to one JSON manifest.",
)

def k3s_tcg_vm_image_manifest(name):
    """Declare only the KubeVirt and CI guest image layouts for a standalone TCG VMI test."""
    image_manifest(
        name = name,
        testonly = True,
        fixture = {},
        pki = {},
        vm = VM_IMAGES,
        guest = VM_GUEST_BOOT_IMAGE | VM_GUEST_ROOT_IMAGE | VM_RUNNER_DISK_IMAGE,
    )

def k3s_pki_image_manifest(name):
    """Declare the retained PKI controllers without application or guest images."""
    image_manifest(name = name, testonly = True, pki = PKI_IMAGES)

def _image_seed_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".tar")
    inputs = []
    args = ctx.actions.args()
    args.add(out)
    for target, reference in ctx.attr.images.items():
        layouts = target[DefaultInfo].files.to_list()
        if len(layouts) != 1:
            fail("expected one OCI layout from %s, got %s" % (target.label, layouts))
        layout = layouts[0]
        inputs.append(layout)
        args.add(reference)
        args.add(layout.path)
    ctx.actions.run(
        inputs = inputs,
        outputs = [out],
        executable = ctx.attr._tool[DefaultInfo].files_to_run,
        arguments = [args],
        mnemonic = "PackK3sImageSeed",
    )
    return [DefaultInfo(files = depset([out]))]

image_seed = rule(
    implementation = _image_seed_impl,
    attrs = {
        "images": attr.label_keyed_string_dict(mandatory = True),
        "_tool": attr.label(
            default = "//agentplane/system_tests/k3s:image_seed_bin",
            executable = True,
            cfg = "exec",
        ),
    },
)
