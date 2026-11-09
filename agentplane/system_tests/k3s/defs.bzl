"""k3s test suite macro and shared infrastructure definitions."""

load("@artifact_pins//:pins.bzl", "CERT_MANAGER_V1_21_2_RELEASE_SHA256", "HELM_3_20_2_LINUX_AMD64_SHA256", "K3S_LINUX_AMD64_SHA256", "KUBEVIRT_OPERATOR_BUNDLE_SHA256", "KYVERNO_INSTALL_SHA256", "TRUST_MANAGER_CHART_V0_25_0_SHA256")
load("@aspect_bazel_lib//lib:paths.bzl", "to_rlocation_path")
load("@image_pins//:pins.bzl", "CERT_MANAGER_CAINJECTOR_DIGEST", "CERT_MANAGER_CAINJECTOR_REFERENCE", "CERT_MANAGER_CONTROLLER_DIGEST", "CERT_MANAGER_CONTROLLER_REFERENCE", "CERT_MANAGER_WEBHOOK_DIGEST", "CERT_MANAGER_WEBHOOK_REFERENCE", "GENERIC_DEVICE_PLUGIN_REFERENCE", "KUBEVIRT_VIRT_API_DIGEST", "KUBEVIRT_VIRT_API_REFERENCE", "KUBEVIRT_VIRT_CONTROLLER_DIGEST", "KUBEVIRT_VIRT_CONTROLLER_REFERENCE", "KUBEVIRT_VIRT_HANDLER_DIGEST", "KUBEVIRT_VIRT_HANDLER_REFERENCE", "KUBEVIRT_VIRT_LAUNCHER_DIGEST", "KUBEVIRT_VIRT_LAUNCHER_REFERENCE", "KUBEVIRT_VIRT_OPERATOR_DIGEST", "KUBEVIRT_VIRT_OPERATOR_REFERENCE", "KYVERNO_KYVERNOPRE_DIGEST", "KYVERNO_KYVERNOPRE_REFERENCE", "KYVERNO_KYVERNO_DIGEST", "KYVERNO_KYVERNO_REFERENCE", "TRUST_MANAGER_DIGEST", "TRUST_MANAGER_REFERENCE")
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

_FIXTURE = "//agentplane/system_tests/deployment:rendered/local-model.json"
_MCP_FIXTURE = "//agentplane/system_tests/deployment:rendered/local-model-mcp-fixtures.json"
_VM_FIXTURE = "//agentplane/system_tests/deployment:rendered/local-model-vm.json"
_FIRECRACKER_FIXTURE = "//agentplane/system_tests/deployment:rendered/local-model-firecracker.json"
_REAL_MODEL_FIXTURE = "//agentplane/system_tests/deployment:rendered/public-litellm.json"
_CERT_MANAGER_INSTALL = "@cert_manager_v1_21_2_release//file"
_TRUST_MANAGER_INSTALL = "//agentplane/system_tests/k3s/pki:trust_manager_install_yaml"

# Each entry is an OCI image target mapped to the reference used when importing it into k3s.
# Keep suite selection here so its fixture, PKI, VM and guest import groups stay explicit.
# Fixture and guest references stay under baseline.FIXTURE_IMAGE_REPOSITORY: a stack removes
# such images that its manifest does not declare.
_SERVICE_IMAGES = {
    "//agentplane/action_service:service_image": "git.allegedly.works/ducktape-ci/agentplane-action-service:unset",
    "//agentplane/app:image": "git.allegedly.works/ducktape-ci/agentplane-app:unset",
    "//agentplane/egress/cmd:image": "git.allegedly.works/ducktape-ci/agentplane-egress-proxy:unset",
    "//agentplane/egress:sidecar_image": "git.allegedly.works/ducktape-ci/agentplane-egress-sidecar:unset",
    "//agentplane/history_service:service_image": "git.allegedly.works/ducktape-ci/agentplane-history-service:unset",
    "//agentplane/llm_ingress:image": "git.allegedly.works/ducktape-ci/agentplane-llm-ingress:unset",
    "//agentplane/mcp_edge:service_image": "git.allegedly.works/ducktape-ci/agentplane-mcp-edge:unset",
    "//agentplane/notification_service:service_image": "git.allegedly.works/ducktape-ci/agentplane-notification-service:unset",
    "//agentplane/policy_controller:image": "git.allegedly.works/ducktape-ci/agentplane-policy-controller:unset",
    "//agentplane/runner_gateway:image": "git.allegedly.works/ducktape-ci/agentplane-runner-gateway:unset",
    "//agentplane/sandbox_controller:image": "git.allegedly.works/ducktape-ci/agentplane-sandbox-controller:unset",
    "@postgres_18_minimal_linux_amd64": "git.allegedly.works/ducktape-ci/agentplane-postgres:unset",
}

# The environment-tools variant a Pod-template suite's Sandboxes run, by the tools its scenarios use
# (agentplane/system_tests/deployment/BUILD.bazel). Every variant imports under the one reference the
# fixture's Pod template names.
_ENVIRONMENT_TOOLS_REFERENCE = "git.allegedly.works/ducktape-ci/environment-tools:unset"
_ENVIRONMENT_TOOLS_IMAGES = {
    "codex": "//agentplane/system_tests/deployment:environment_tools_codex_image",
    "harnesses": "//agentplane/system_tests/deployment:environment_tools_harnesses_image",
    "egress": "//agentplane/system_tests/deployment:environment_tools_egress_image",
    "real_model": "//agentplane/system_tests/deployment:environment_tools_real_model_image",
}
_MCP_IMAGES = {
    "//agentplane/central_sampler/cmd:service_image": "git.allegedly.works/ducktape-ci/agentplane-central-sampler:unset",
    "@dex_linux_amd64": "git.allegedly.works/ducktape-ci/agentplane-dex:unset",
    "@mcp_everything_linux_amd64": "git.allegedly.works/ducktape-ci/agentplane-mcp-everything:unset",
    "//agentplane/action_service/testing:oauth_mcp_server_image": "git.allegedly.works/ducktape-ci/agentplane-oauth-fixture:unset",
}
_PKI_IMAGES = {
    "@cert_manager_controller_linux_amd64": CERT_MANAGER_CONTROLLER_REFERENCE,
    "@cert_manager_webhook_linux_amd64": CERT_MANAGER_WEBHOOK_REFERENCE,
    "@cert_manager_cainjector_linux_amd64": CERT_MANAGER_CAINJECTOR_REFERENCE,
    "@trust_manager_linux_amd64": TRUST_MANAGER_REFERENCE,
}
_VM_IMAGES = {
    "@kubevirt_virt_api_linux_amd64": KUBEVIRT_VIRT_API_REFERENCE,
    "@kubevirt_virt_controller_linux_amd64": KUBEVIRT_VIRT_CONTROLLER_REFERENCE,
    "@kubevirt_virt_handler_linux_amd64": KUBEVIRT_VIRT_HANDLER_REFERENCE,
    "@kubevirt_virt_launcher_linux_amd64": KUBEVIRT_VIRT_LAUNCHER_REFERENCE,
    "@kubevirt_virt_operator_linux_amd64": KUBEVIRT_VIRT_OPERATOR_REFERENCE,
    "@kyverno_kyverno_linux_amd64": KYVERNO_KYVERNO_REFERENCE,
    "@kyverno_kyvernopre_linux_amd64": KYVERNO_KYVERNOPRE_REFERENCE,
}
_VM_GUEST_BOOT_IMAGE = {
    "//agentplane/system_tests/vm/guest:boot_image": "git.allegedly.works/ducktape-ci/agentplane-vm-guest-boot:unset",
}
_VM_GUEST_ROOT_IMAGE = {
    "//agentplane/system_tests/vm/guest:root_image": "git.allegedly.works/ducktape-ci/agentplane-vm-guest-root:unset",
}
_VM_GUEST_ROOT_PROFILE_IMAGE = {
    "//agentplane/system_tests/vm/guest:root_profile_image": "git.allegedly.works/ducktape-ci/agentplane-vm-guest-root-profile:unset",
}
_RUNNER_DISK_IMAGE = {
    "//agentplane/env/images:disk_image": "git.allegedly.works/ducktape-ci/agentplane-runner-disk:unset",
}
_DELETION_TEST_DISK_IMAGE = {
    "//agentplane/system_tests/deployment:deletion_test_disk_image": "git.allegedly.works/ducktape-ci/agentplane-runner-disk:unset",
}
_VM_RUNNER_DISK_IMAGE = {
    "//agentplane/system_tests/vm:test_runner_disk_image": "git.allegedly.works/ducktape-ci/agentplane-runner-disk:unset",
}

_FIRECRACKER_GUEST_IMAGES = {
    "//agentplane/firecracker:image": "git.allegedly.works/ducktape-ci/agentplane-firecracker-launcher:unset",
    "//agentplane/system_tests/firecracker:boot_image": "git.allegedly.works/ducktape-ci/agentplane-environment-firecracker:unset",
}

def _merge_image_maps(*groups):
    images = {}
    for group in groups:
        for image, reference in group.items():
            if image in images:
                fail("duplicate image target: " + image)
            images[image] = reference
    return images

# Each preset explicitly owns the environment its selected scenarios need. Dispatcher failover
# uses the pod preset in a dedicated suite because it mutates shared controller Deployments.
_STACKS = {
    "pod": struct(fixture = _FIXTURE, vm = False, firecracker = False, real_model = False, images = {}),
    "deletion": struct(fixture = _FIXTURE, vm = False, firecracker = False, real_model = False, images = {}),
    "mcp": struct(fixture = _MCP_FIXTURE, vm = False, firecracker = False, real_model = False, images = _MCP_IMAGES),
    "vm": struct(fixture = _VM_FIXTURE, vm = True, firecracker = False, real_model = False, images = {}),
    "firecracker": struct(fixture = _FIRECRACKER_FIXTURE, vm = False, firecracker = True, real_model = False, images = {}),
    "real_model": struct(fixture = _REAL_MODEL_FIXTURE, vm = False, firecracker = False, real_model = True, images = {}),
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

def k3s_suite(name, srcs, deps, stack, environment_tools = None, tags = [], profile_guest = False, boot_evidence = False, shutdown_diagnostic = False, require_kvm = False, main = None):
    """Run one suite with fresh fixture state and the selected executor lifecycle.

    `environment_tools` names the `_ENVIRONMENT_TOOLS_IMAGES` variant a Pod-template stack's
    Sandboxes run; VM and Firecracker stacks take none.
    """
    if stack not in _STACKS:
        fail("unknown k3s stack: " + stack)
    if profile_guest and stack != "vm":
        fail("profile_guest requires the VM stack")
    if boot_evidence and stack != "vm":
        fail("boot_evidence requires the VM stack")
    if shutdown_diagnostic and stack != "vm":
        fail("shutdown_diagnostic requires the VM stack")
    config = _STACKS[stack]
    if require_kvm and not (config.vm or config.firecracker):
        fail("require_kvm requires the VM or Firecracker stack")
    if config.firecracker and not require_kvm:
        fail("the Firecracker stack requires require_kvm = True")
    runner_disk_image = _DELETION_TEST_DISK_IMAGE if stack == "deletion" else _VM_RUNNER_DISK_IMAGE if config.vm else _RUNNER_DISK_IMAGE
    if config.vm or config.firecracker:
        if environment_tools != None:
            fail("environment_tools is only for Pod-template stacks")
        environment_tools_image = {}
    elif environment_tools not in _ENVIRONMENT_TOOLS_IMAGES:
        fail("the %s stack requires environment_tools, one of %s" % (stack, sorted(_ENVIRONMENT_TOOLS_IMAGES)))
    else:
        environment_tools_image = {_ENVIRONMENT_TOOLS_IMAGES[environment_tools]: _ENVIRONMENT_TOOLS_REFERENCE}
    fixture_images = _merge_image_maps(_SERVICE_IMAGES, config.images, environment_tools_image, runner_disk_image)
    images = {"fixture": fixture_images, "pki": _PKI_IMAGES, "vm": {}, "guest": {}}
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
        if config.vm:
            images["vm"] = _VM_IMAGES
            guest_root_image = _VM_GUEST_ROOT_PROFILE_IMAGE if profile_guest else _VM_GUEST_ROOT_IMAGE
            images["guest"] = _merge_image_maps(_VM_GUEST_BOOT_IMAGE, guest_root_image)
        else:
            images["guest"] = _FIRECRACKER_GUEST_IMAGES
            images["vm"] = {"@generic_device_plugin_linux_amd64": GENERIC_DEVICE_PLUGIN_REFERENCE}
            env["KVM_DEVICE_PLUGIN_IMAGE"] = GENERIC_DEVICE_PLUGIN_REFERENCE
        if config.vm:
            data += ["@kubevirt_operator_bundle//file", "@kyverno_install//file"]
            env |= {
                "KUBEVIRT_OPERATOR_RLOCATION": "$(rlocationpath @kubevirt_operator_bundle//file)",
                "KYVERNO_INSTALL_RLOCATION": "$(rlocationpath @kyverno_install//file)",
            }
        env |= {
            "AGENTPLANE_K3S_REQUIRE_KVM": "1" if require_kvm else "0",
            "AGENTPLANE_VM_BOOT_EVIDENCE": "1" if boot_evidence else "0",
            "AGENTPLANE_VM_SHUTDOWN_DIAGNOSTIC": "1" if shutdown_diagnostic else "0",
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

def k3s_tcg_vm_image_manifest(name):
    """Declare only the KubeVirt and CI guest image layouts for a standalone TCG VMI test."""
    image_manifest(
        name = name,
        testonly = True,
        fixture = {},
        pki = {},
        vm = _VM_IMAGES,
        guest = _merge_image_maps(_VM_GUEST_BOOT_IMAGE, _VM_GUEST_ROOT_IMAGE, _VM_RUNNER_DISK_IMAGE),
    )

def k3s_pki_image_manifest(name):
    """Declare the retained PKI controllers without application or guest images."""
    image_manifest(name = name, testonly = True, pki = _PKI_IMAGES)
