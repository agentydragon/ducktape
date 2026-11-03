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

_HARNESSES_LAYOUT = "//agentplane/system_tests/k3s:harnesses/environment_tools_layout"
_FIXTURE = "//agentplane/system_tests/k3s:rendered/local-model.json"
_MCP_FIXTURE = "//agentplane/system_tests/k3s:rendered/local-model-mcp-fixtures.json"
_MCP_IMAGES = "//agentplane/system_tests/k3s:mcp_images"
_VM_FIXTURE = "//agentplane/system_tests/k3s:rendered/local-model-vm.json"
_REAL_MODEL_FIXTURE = "//agentplane/system_tests/k3s:rendered/public-litellm.json"
_CERT_MANAGER_INSTALL = "@cert_manager_v1_21_2_release//file"
_TRUST_MANAGER_INSTALL = "//agentplane/system_tests/k3s/pki:trust_manager_install_yaml"

# Each preset explicitly owns the environment its selected scenarios need. Dispatcher failover
# uses the pod preset in a dedicated suite because it mutates shared controller Deployments.
_STACKS = {
    "pod": struct(fixture = _FIXTURE, vm = False, real_model = False, images = [_HARNESSES_LAYOUT]),
    "mcp": struct(fixture = _MCP_FIXTURE, vm = False, real_model = False, images = [_HARNESSES_LAYOUT, _MCP_IMAGES]),
    "vm": struct(fixture = _VM_FIXTURE, vm = True, real_model = False, images = []),
    "real_model": struct(fixture = _REAL_MODEL_FIXTURE, vm = False, real_model = True, images = [_HARNESSES_LAYOUT]),
}

def k3s_suite(name, srcs, deps, stack, tags = [], profile_guest = False, boot_evidence = False, main = None, kvm_variant = False):
    """Run one suite with retained infrastructure and fresh per-action application state."""
    if stack not in _STACKS:
        fail("unknown k3s stack: " + stack)
    if profile_guest and stack != "vm":
        fail("profile_guest requires the VM stack")
    if boot_evidence and stack != "vm":
        fail("boot_evidence requires the VM stack")
    if kvm_variant and stack != "vm":
        fail("kvm_variant requires the VM stack")
    config = _STACKS[stack]
    images = {
        "fixture": [
            "//agentplane/system_tests/k3s:service_images",
            "//agentplane/system_tests/k3s:vm_runner_disk_image" if config.vm else "//agentplane/system_tests/k3s:runner_disk_image",
        ] + config.images,
        "pki": ["//agentplane/system_tests/k3s/pki:images"],
        "vm": [],
        "guest": [],
    }
    data = [config.fixture, K3S_BINARY, _CERT_MANAGER_INSTALL, _TRUST_MANAGER_INSTALL, "//agentplane/crds"]
    env = {
        "K3S_BINARY_RLOCATION": K3S_BINARY_RLOCATION,
        "CERT_MANAGER_INSTALL_RLOCATION": "$(rlocationpath " + _CERT_MANAGER_INSTALL + ")",
        "TRUST_MANAGER_INSTALL_RLOCATION": "$(rlocationpath " + _TRUST_MANAGER_INSTALL + ")",
        "K3S_FIXTURE_RLOCATION": "$(rlocationpath " + config.fixture + ")",
        "PYTEST_PLUGINS": "agentplane.system_tests.k3s.system_test_target",
    }
    properties = {}
    suite_tags = K3S_TAGS + tags
    if config.vm:
        # Three 1.5 GiB guests boot alongside k3s services; request eight estimated compute units.
        properties["EstimatedComputeUnits"] = "8"
        images["vm"] = ["//agentplane/system_tests/k3s:vm_images"]
        images["guest"] = [
            "//agentplane/system_tests/k3s/guest:vm_guest_boot_layout",
            "//agentplane/system_tests/k3s/guest:vm_guest_root_profile_layout" if profile_guest else "//agentplane/system_tests/k3s/guest:vm_guest_root_layout",
        ]
        data += ["@kubevirt_operator_bundle//file", "@kyverno_install//file"]
        env |= {
            "KUBEVIRT_OPERATOR_RLOCATION": "$(rlocationpath @kubevirt_operator_bundle//file)",
            "KYVERNO_INSTALL_RLOCATION": "$(rlocationpath @kyverno_install//file)",
            "AGENTPLANE_VM_BOOT_EVIDENCE": "1" if boot_evidence else "0",
            "K3S_PROFILE_GUEST_ROOT": "1" if profile_guest else "0",
        }

    env["K3S_IMAGE_MANIFESTS"] = json.encode({
        group: ["$(rlocationpath " + label + ")" for label in manifests]
        for group, manifests in images.items()
    })
    data += [label for manifests in images.values() for label in manifests]

    if config.real_model:
        properties["test.env-secrets"] = "OPENAI_API_KEY"
        suite_tags.append("live_openai_api")

    for kvm in [False, True] if kvm_variant else [False]:
        if kvm:
            isolation = {
                "test.Pool": "wyrm2-kvm",
                "test.use-self-hosted-executors": "true",
                "test.workload-isolation-type": "oci",
                "test.init-dockerd": "false",
                "test.recycle-runner": "false",
            }
        else:
            # Retained infrastructure snapshots belong to the hosted Firecracker runner.
            isolation = K3S_PERSISTENCE_PROPERTIES | {
                "test.runner-recycling-key": k3s_recycling_key(vm = config.vm),
            }
        py_test(
            name = name + ".kvm" if kvm else name,
            size = "enormous",
            srcs = srcs,
            main = main or name + ".py",
            data = data,
            env = env | {"K3S_VM_ACCELERATOR": "kvm" if kvm else "tcg"},
            exec_properties = K3S_EXEC_PROPERTIES | properties | isolation,
            tags = suite_tags + (["manual"] if kvm and "manual" not in suite_tags else []),
            deps = deps + [
                "//agentplane/system_tests/k3s:system_test_target",
            ],
        )
