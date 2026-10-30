"""Executable suites: explicit scenario modules sharing one configured k3s stack."""

load("//agentplane/system_tests/k3s:defs.bzl", "K3S_BINARY", "K3S_BINARY_RLOCATION", "K3S_EXEC_PROPERTIES", "K3S_SERVICE_LAYOUTS", "K3S_TAGS", "K3S_VM_DATA", "K3S_VM_ENV")
load("//devinfra/python:defs.bzl", "py_test")

_HARNESSES_LAYOUT = "//agentplane/system_tests/k3s:harnesses/runner_layout"
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
    "vm": struct(fixture = _VM_FIXTURE, vm = True, real_model = False, images = [_SAMPLER_LAYOUT]),
    "real_model": struct(fixture = _REAL_MODEL_FIXTURE, vm = False, real_model = True, images = [_HARNESSES_LAYOUT]),
}

def k3s_runtime(stack):
    """Artifacts and RBE settings for a stack, independent of its test runner."""
    if stack not in _STACKS:
        fail("unknown k3s stack: " + stack)
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
    }
    properties = {}
    suite_tags = list(K3S_TAGS)
    if config.vm:
        # Six 1.5 GiB guests boot alongside k3s services; request the 20 GB worker estimate.
        properties["EstimatedComputeUnits"] = "8"
        guests = [
            "//agentplane/system_tests/k3s/guest:vm_guest_boot_layout",
            "//agentplane/system_tests/k3s/guest:vm_guest_root_layout",
        ]
        data += K3S_VM_DATA + guests
        env |= K3S_VM_ENV | {"K3S_GUEST_IMAGE_LAYOUTS": " ".join(["$(rlocationpath " + image + ")" for image in guests])}

        # VM layers stay on disk; Pod runners use a different tmpfs image store.
        properties["test.runner-recycling-key"] = "k3s-vm"
    if config.real_model:
        properties["test.env-secrets"] = "OPENAI_API_KEY"
        suite_tags.append("live_openai_api")
    return struct(
        data = data,
        env = env,
        exec_properties = K3S_EXEC_PROPERTIES | properties,
        tags = suite_tags,
    )

def k3s_suite(name, srcs, deps, stack, tags = []):
    """Run a Python suite entrypoint against one session-scoped k3s stack."""
    runtime = k3s_runtime(stack)
    py_test(
        name = name,
        size = "enormous",
        srcs = srcs,
        data = runtime.data,
        env = runtime.env | {"PYTEST_PLUGINS": "agentplane.system_tests.k3s.system_test_target"},
        exec_properties = runtime.exec_properties,
        tags = runtime.tags + tags,
        deps = deps + [
            "//agentplane/system_tests/k3s:system_test_target",
        ],
    )
