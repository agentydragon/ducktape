"""k3s test suite macro and shared infrastructure definitions."""

load("@artifact_pins//:pins.bzl", "CERT_MANAGER_V1_21_2_RELEASE_SHA256", "HELM_3_20_2_LINUX_AMD64_SHA256", "K3S_LINUX_AMD64_SHA256", "KUBEVIRT_OPERATOR_BUNDLE_SHA256", "KYVERNO_INSTALL_SHA256", "TRUST_MANAGER_CHART_V0_25_0_SHA256")
load("@image_pins//:pins.bzl", "CERT_MANAGER_CAINJECTOR_DIGEST", "CERT_MANAGER_CONTROLLER_DIGEST", "CERT_MANAGER_WEBHOOK_DIGEST", "KUBEVIRT_VIRT_API_DIGEST", "KUBEVIRT_VIRT_CONTROLLER_DIGEST", "KUBEVIRT_VIRT_HANDLER_DIGEST", "KUBEVIRT_VIRT_LAUNCHER_DIGEST", "KUBEVIRT_VIRT_OPERATOR_DIGEST", "KYVERNO_KYVERNOPRE_DIGEST", "KYVERNO_KYVERNO_DIGEST", "TRUST_MANAGER_DIGEST")
load("//devinfra/oci:defs.bzl", "OciImageGroupInfo")
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
    # Leave remote-snapshot-save-policy unset: forced saves overload BuildBuddy's snapshot infrastructure.
    "test.snapshot-read-policy": "newest",
}

# Match the dedicated VM worker's four-vCPU budget.
KVM_EXEC_PROPERTIES = {
    "test.EstimatedComputeUnits": "4",
    "test.Pool": "wyrm2-kvm-vm",
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

_HARNESSES_LAYOUT = "//agentplane/system_tests/deployment:harnesses/environment_tools_layout"
_FIXTURE = "//agentplane/system_tests/deployment:rendered/local-model.json"
_MCP_FIXTURE = "//agentplane/system_tests/deployment:rendered/local-model-mcp-fixtures.json"
_MCP_IMAGES = "//agentplane/system_tests/deployment:mcp_images"
_VM_FIXTURE = "//agentplane/system_tests/deployment:rendered/local-model-vm.json"
_FIRECRACKER_FIXTURE = "//agentplane/system_tests/deployment:rendered/local-model-firecracker.json"
_REAL_MODEL_FIXTURE = "//agentplane/system_tests/deployment:rendered/public-litellm.json"
_CERT_MANAGER_INSTALL = "@cert_manager_v1_21_2_release//file"
_TRUST_MANAGER_INSTALL = "//agentplane/system_tests/k3s/pki:trust_manager_install_yaml"

# Each preset explicitly owns the environment its selected scenarios need. Dispatcher failover
# uses the pod preset in a dedicated suite because it mutates shared controller Deployments.
_STACKS = {
    "pod": struct(fixture = _FIXTURE, vm = False, firecracker = False, real_model = False, images = [_HARNESSES_LAYOUT]),
    "deletion": struct(fixture = _FIXTURE, vm = False, firecracker = False, real_model = False, images = [_HARNESSES_LAYOUT]),
    "mcp": struct(fixture = _MCP_FIXTURE, vm = False, firecracker = False, real_model = False, images = [_HARNESSES_LAYOUT, _MCP_IMAGES]),
    "vm": struct(fixture = _VM_FIXTURE, vm = True, firecracker = False, real_model = False, images = []),
    "firecracker": struct(fixture = _FIRECRACKER_FIXTURE, vm = False, firecracker = True, real_model = False, images = [], guest_images = "//agentplane/system_tests/firecracker:images"),
    "real_model": struct(fixture = _REAL_MODEL_FIXTURE, vm = False, firecracker = False, real_model = True, images = [_HARNESSES_LAYOUT]),
}

def _image_manifest_impl(ctx):
    groups = {}
    runfiles = ctx.runfiles()
    for name in ["fixture", "pki", "vm", "guest"]:
        references = {}
        for group in getattr(ctx.attr, name):
            for reference, location in group[OciImageGroupInfo].references.items():
                if reference in references:
                    fail("duplicate image reference in %s: %s" % (name, reference))
                references[reference] = location
            runfiles = runfiles.merge(group[DefaultInfo].default_runfiles)
        groups[name] = references
    out = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(out, json.encode(groups))
    return [DefaultInfo(files = depset([out]), runfiles = runfiles.merge(ctx.runfiles([out])))]

image_manifest = rule(
    implementation = _image_manifest_impl,
    attrs = {name: attr.label_list(providers = [OciImageGroupInfo]) for name in ["fixture", "pki", "vm", "guest"]},
    doc = "Combine image groups into a single grouped OCI layout manifest and runfiles closure.",
)

def k3s_suite(name, srcs, deps, stack, tags = [], profile_guest = False, boot_evidence = False, require_kvm = False, main = None):
    """Run one suite with fresh fixture state and the selected executor lifecycle."""
    if stack not in _STACKS:
        fail("unknown k3s stack: " + stack)
    if profile_guest and stack != "vm":
        fail("profile_guest requires the VM stack")
    if boot_evidence and stack != "vm":
        fail("boot_evidence requires the VM stack")
    config = _STACKS[stack]
    if require_kvm and not (config.vm or config.firecracker):
        fail("require_kvm requires the VM or Firecracker stack")
    if config.firecracker and not require_kvm:
        fail("the Firecracker stack requires require_kvm = True")
    images = {
        "fixture": [
            "//agentplane/system_tests/deployment:service_images",
            "//agentplane/system_tests/deployment:deletion_test_disk" if stack == "deletion" else "//agentplane/system_tests/vm:vm_runner_disk_image" if config.vm else "//agentplane/system_tests/deployment:runner_disk_image",
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
        "PYTEST_PLUGINS": "agentplane.system_tests.deployment.system_test_target" + (",agentplane.system_tests.vm.pytest_plugin" if config.vm else ",agentplane.system_tests.firecracker.pytest_plugin" if config.firecracker else ""),
    }
    properties = {}
    suite_tags = K3S_TAGS + tags
    if config.vm or config.firecracker:
        # A guest runs alongside k3s services; reserve the VM lane's larger worker budget.
        properties["EstimatedComputeUnits"] = "8"
        images["vm"] = ["//agentplane/system_tests/vm:vm_images"]
        if config.vm:
            images["guest"] = [
                "//agentplane/system_tests/vm/guest:vm_guest_boot_layout",
                "//agentplane/system_tests/vm/guest:vm_guest_root_profile_layout" if profile_guest else "//agentplane/system_tests/vm/guest:vm_guest_root_layout",
            ]
        else:
            images["guest"] = [config.guest_images]
        data += ["@kubevirt_operator_bundle//file", "@kyverno_install//file"]
        env |= {
            "KUBEVIRT_OPERATOR_RLOCATION": "$(rlocationpath @kubevirt_operator_bundle//file)",
            "KYVERNO_INSTALL_RLOCATION": "$(rlocationpath @kyverno_install//file)",
            "AGENTPLANE_K3S_REQUIRE_KVM": "1" if require_kvm else "0",
            "AGENTPLANE_VM_BOOT_EVIDENCE": "1" if boot_evidence else "0",
            "K3S_PROFILE_GUEST_ROOT": "1" if profile_guest else "0",
        }

    manifest = name + "_image_manifest"
    image_manifest(name = manifest, testonly = True, **images)
    env["K3S_IMAGE_MANIFEST_RLOCATION"] = "$(rlocationpath :" + manifest + ")"
    data.append(":" + manifest)

    if config.real_model:
        properties["test.env-secrets"] = "OPENAI_API_KEY"
        suite_tags.append("live_openai_api")

    if require_kvm:
        properties |= KVM_EXEC_PROPERTIES
    else:
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
            "//agentplane/system_tests/deployment:system_test_target",
        ] + (["//agentplane/system_tests/vm:pytest_plugin"] if config.vm else ["//agentplane/system_tests/firecracker:pytest_plugin"] if config.firecracker else []),
    )
