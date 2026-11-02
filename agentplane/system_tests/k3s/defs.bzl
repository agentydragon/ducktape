"""k3s test suite macro and shared infrastructure definitions."""

load("@artifact_pins//:pins.bzl", "CERT_MANAGER_V1_21_2_RELEASE_SHA256", "HELM_3_20_2_LINUX_AMD64_SHA256", "K3S_LINUX_AMD64_SHA256", "KUBEVIRT_OPERATOR_BUNDLE_SHA256", "KYVERNO_INSTALL_SHA256", "TRUST_MANAGER_CHART_V0_25_0_SHA256")
load("@image_pins//:pins.bzl", "CERT_MANAGER_CAINJECTOR_DIGEST", "CERT_MANAGER_CONTROLLER_DIGEST", "CERT_MANAGER_WEBHOOK_DIGEST", "KUBEVIRT_VIRT_API_DIGEST", "KUBEVIRT_VIRT_CONTROLLER_DIGEST", "KUBEVIRT_VIRT_HANDLER_DIGEST", "KUBEVIRT_VIRT_LAUNCHER_DIGEST", "KUBEVIRT_VIRT_OPERATOR_DIGEST", "KYVERNO_KYVERNOPRE_DIGEST", "KYVERNO_KYVERNO_DIGEST", "TRUST_MANAGER_DIGEST")
load("//devinfra/python:defs.bzl", "py_test")

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
    return "k3s-persistence-v7-" + "-".join(
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

_HARNESSES_LAYOUT = "//agentplane/system_tests/k3s:harnesses/environment_tools_layout"
_FIXTURE = "//agentplane/system_tests/k3s:rendered/local-model.json"
_MCP_FIXTURE = "//agentplane/system_tests/k3s:rendered/local-model-mcp-fixtures.json"
_MCP_LAYOUTS = [
    "//agentplane/system_tests/k3s:mcp_fixtures/dex_layout",
    "//agentplane/system_tests/k3s:mcp_fixtures/mcp_everything_layout",
    "//agentplane/system_tests/k3s:mcp_fixtures/oauth_fixture_layout",
]
_VM_FIXTURE = "//agentplane/system_tests/k3s:rendered/local-model-vm.json"
_SAMPLER_LAYOUT = "//agentplane/system_tests/k3s:central_sampler_layout"
_REAL_MODEL_FIXTURE = "//agentplane/system_tests/k3s:rendered/public-litellm.json"
_CERT_MANAGER_INSTALL = "//agentplane/system_tests/k3s/pki:cert_manager_install_yaml"
_TRUST_MANAGER_INSTALL = "//agentplane/system_tests/k3s/pki:trust_manager_install_yaml"

# Each preset explicitly owns the environment its selected scenarios need. Dispatcher failover
# uses the pod preset in a dedicated suite because it mutates shared controller Deployments.
_STACKS = {
    "pod": struct(fixture = _FIXTURE, vm = False, real_model = False, images = [_HARNESSES_LAYOUT]),
    "mcp": struct(fixture = _MCP_FIXTURE, vm = False, real_model = False, images = [_HARNESSES_LAYOUT, _SAMPLER_LAYOUT] + _MCP_LAYOUTS),
    "vm": struct(fixture = _VM_FIXTURE, vm = True, real_model = False, images = []),
    "real_model": struct(fixture = _REAL_MODEL_FIXTURE, vm = False, real_model = True, images = [_HARNESSES_LAYOUT]),
}

def k3s_suite(name, srcs, deps, stack, tags = [], profile_guest = False, boot_evidence = False, main = None):
    """Run one suite with retained infrastructure and fresh per-action application state."""
    if stack not in _STACKS:
        fail("unknown k3s stack: " + stack)
    if profile_guest and stack != "vm":
        fail("profile_guest requires the VM stack")
    if boot_evidence and stack != "vm":
        fail("boot_evidence requires the VM stack")
    config = _STACKS[stack]
    layouts = K3S_SERVICE_LAYOUTS + config.images
    data = [config.fixture, K3S_BINARY, _CERT_MANAGER_INSTALL, _TRUST_MANAGER_INSTALL] + layouts
    env = {
        "K3S_BINARY_RLOCATION": K3S_BINARY_RLOCATION,
        "CERT_MANAGER_INSTALL_RLOCATION": "$(rlocationpath " + _CERT_MANAGER_INSTALL + ")",
        "TRUST_MANAGER_INSTALL_RLOCATION": "$(rlocationpath " + _TRUST_MANAGER_INSTALL + ")",
        "K3S_FIXTURE_RLOCATION": "$(rlocationpath " + config.fixture + ")",
        "K3S_IMAGE_LAYOUTS": " ".join(["$(rlocationpath " + layout + ")" for layout in layouts]),
        "K3S_GUEST_IMAGE_LAYOUTS": "",
        "PYTEST_PLUGINS": "agentplane.system_tests.k3s.system_test_target",
    }
    properties = {}
    suite_tags = K3S_TAGS + tags
    if config.vm:
        # Six 1.5 GiB guests boot alongside k3s services; request the 20 GB worker estimate.
        properties["EstimatedComputeUnits"] = "8"
        guests = [
            "//agentplane/system_tests/k3s/guest:vm_guest_boot_layout",
            "//agentplane/system_tests/k3s/guest:vm_guest_root_profile_layout" if profile_guest else "//agentplane/system_tests/k3s/guest:vm_guest_root_layout",
        ]
        data += K3S_VM_DATA + guests
        env |= K3S_VM_ENV | {
            "AGENTPLANE_VM_BOOT_EVIDENCE": "1" if boot_evidence else "0",
            "K3S_GUEST_IMAGE_LAYOUTS": " ".join(["$(rlocationpath " + image + ")" for image in guests]),
            "K3S_PROFILE_GUEST_ROOT": "1" if profile_guest else "0",
        }

    if config.real_model:
        properties["test.env-secrets"] = "OPENAI_API_KEY"
        suite_tags.append("live_openai_api")

    # VM layers stay on disk; Pod/MCP/real-model suites use a separate tmpfs baseline.
    properties |= K3S_PERSISTENCE_PROPERTIES | {
        "test.runner-recycling-key": k3s_recycling_key(vm = config.vm),
    }
    py_test(
        name = name,
        size = "enormous",
        srcs = srcs,
        main = main,
        data = data,
        env = env,
        exec_properties = K3S_EXEC_PROPERTIES | properties,
        tags = suite_tags,
        deps = deps + [
            "//agentplane/system_tests/k3s:system_test_target",
        ],
    )
