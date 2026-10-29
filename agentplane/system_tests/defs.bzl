"""A system test scenario: run on a k3s stack of its own, opted in with VM Sandboxes or a real model."""

load("//agentplane/system_tests/k3s:defs.bzl", "K3S_BINARY", "K3S_BINARY_RLOCATION", "K3S_EXEC_PROPERTIES", "K3S_TAGS", "K3S_VM_DATA", "K3S_VM_ENV")
load("//devinfra/python:defs.bzl", "py_library", "py_test")

_HARNESSES_LAYOUT = "//agentplane/system_tests/k3s:harnesses/runner_layout"

# The k3s target's runner image, by what its scenarios run.
_RUNNER_LAYOUTS = {
    # No harness, so the image imports faster. Only for scenarios that run no turn: activating a
    # Codex Session starts the harness binary through the isolated executor's companion, which fails
    # on an image without one (agentplane/runner/executor/).
    "model_free": "//agentplane/system_tests/k3s:model_free/runner_layout",
    "scripted": _HARNESSES_LAYOUT,
}

# The stack's manifest, LLM ingress forwarding to the scenario's scripted model; and with Dex and the
# MCP servers `test_mcp` calls, whose image layouts the stack then imports too.
_FIXTURE = "//agentplane/system_tests/k3s:rendered/local-model.json"
_MCP_FIXTURE = "//agentplane/system_tests/k3s:rendered/local-model-mcp-fixtures.json"
_MCP_FIXTURE_LAYOUTS = [
    "//agentplane/system_tests/k3s:mcp_fixtures/dex_layout",
    "//agentplane/system_tests/k3s:mcp_fixtures/mcp_everything_layout",
    "//agentplane/system_tests/k3s:mcp_fixtures/oauth_fixture_layout",
]

# The `_vm` target's manifest: the local model's, with VM Sandboxes.
_VM_FIXTURE = "//agentplane/system_tests/k3s:rendered/local-model-vm.json"

# The `_real_model` target's manifest: LLM ingress forwarding to the public LiteLLM with BuildBuddy's
# live-test key (`OPENAI_API_KEY`), decrypted into the test action alone, as for `live_openai_py_test`.
_REAL_MODEL_FIXTURE = "//agentplane/system_tests/k3s:rendered/public-litellm.json"
_REAL_MODEL_SECRET = "OPENAI_API_KEY"

_CERT_MANAGER_INSTALL = "//agentplane/system_tests/k3s/pki:cert_manager_install_yaml"
_TRUST_MANAGER_INSTALL = "//agentplane/system_tests/k3s/pki:trust_manager_install_yaml"

def system_test(name, srcs, deps, data = [], k3s = None, vm_sandboxes = None, real_model = False, mcp_fixtures = False, vm_systemd_proof = False):
    """Generates `name` when `k3s` names its scenarios' kind, `name + "_vm"` when `vm_sandboxes` says
    where it runs, and `name + "_real_model"` when `real_model` is True.

    `name` starts its own k3s stack in one RBE action (`agentplane.system_tests.k3s.system_test_target`) and
    runs the `scripted_model` scenarios and the unmarked ones, in PR CI like any test; `k3s` is
    `"model_free"` when none runs a turn, else `"scripted"`; `mcp_fixtures` adds Dex and the MCP
    servers to its stack. `name + "_vm"` does the same for the `vm_sandboxes` scenarios, which only it
    runs, on a stack with KubeVirt; `vm_sandboxes` is `"ci"` for what only a VM exercises (bring-up,
    networking), else `"manual"`, run by hand. `name + "_real_model"` runs the `real_model` scenarios on
    a k3s stack whose LLM ingress forwards to the public LiteLLM; it is tagged `live_openai_api`, so PR CI
    builds it and `--config=live-openai` runs it.

    `vm_systemd_proof` adds the real-manager kernel proof to the guest startup for its dedicated
    scenario; ordinary VM scenarios use a guest root without the proof binary or override.

    A hidden library owns the source, so all targets share one compiled copy. Gazelle does not
    manage macro rules: exclude the source in the BUILD file and keep `deps` by hand.
    """
    if vm_systemd_proof and vm_sandboxes == None:
        fail("vm_systemd_proof requires vm_sandboxes")
    py_library(
        name = name + "_lib",
        testonly = True,
        srcs = srcs,
        data = data,
        deps = deps,
    )
    common = {
        "main_module": "pytest_bazel",
        "deps": [":" + name + "_lib", "@pypi//pytest_bazel"],
    }
    if k3s != None:
        _k3s_test(
            name = name,
            runner_layout = _RUNNER_LAYOUTS[k3s],
            fixture = _MCP_FIXTURE if mcp_fixtures else _FIXTURE,
            layouts = _MCP_FIXTURE_LAYOUTS if mcp_fixtures else [],
            **common
        )
    if vm_sandboxes != None:
        if vm_sandboxes not in ("ci", "manual"):
            fail("vm_sandboxes is \"ci\" or \"manual\", not %r" % vm_sandboxes)
        guest_root = "//agentplane/system_tests/k3s/guest:" + ("systemd_proof/" if vm_systemd_proof else "") + "vm_guest_root_layout"
        _k3s_test(
            name = name + "_vm",
            # The guest's runner comes from the runner disk; no Sandbox runs the Pod runner image.
            runner_layout = None,
            fixture = _VM_FIXTURE,
            layouts = [],
            data = K3S_VM_DATA + [guest_root],
            env = K3S_VM_ENV | {"K3S_VM_GUEST_ROOT_RLOCATION": "$(rlocationpath " + guest_root + ")"},
            # VM layers stay on disk; sharing pod runners would discard their tmpfs image store.
            exec_properties = {"test.runner-recycling-key": "k3s-vm"},
            tags = ["manual"] if vm_sandboxes == "manual" else [],
            **common
        )
    if real_model:
        _k3s_test(
            name = name + "_real_model",
            runner_layout = _HARNESSES_LAYOUT,
            fixture = _REAL_MODEL_FIXTURE,
            layouts = [],
            env = {"AGENTPLANE_SYSTEM_TEST_MODEL_BACKEND": "real_model"},
            exec_properties = {"test.env-secrets": _REAL_MODEL_SECRET},
            tags = ["live_openai_api"],
            **common
        )

def _k3s_test(name, runner_layout, fixture, layouts, main_module, deps, data = [], env = {}, exec_properties = {}, tags = []):
    py_test(
        name = name,
        size = "enormous",
        data = [
            fixture,
            K3S_BINARY,
            _CERT_MANAGER_INSTALL,
            _TRUST_MANAGER_INSTALL,
        ] + ([runner_layout] if runner_layout != None else []) + layouts + data,
        env = {
            "K3S_BINARY_RLOCATION": K3S_BINARY_RLOCATION,
            "CERT_MANAGER_INSTALL_RLOCATION": "$(rlocationpath " + _CERT_MANAGER_INSTALL + ")",
            "TRUST_MANAGER_INSTALL_RLOCATION": "$(rlocationpath " + _TRUST_MANAGER_INSTALL + ")",
            "AGENTPLANE_SYSTEM_TEST_MODEL_BACKEND": "scripted_model",
            "K3S_FIXTURE_RLOCATION": "$(rlocationpath " + fixture + ")",
            "K3S_MCP_FIXTURE_LAYOUTS": " ".join(["$(rlocationpath " + layout + ")" for layout in layouts]),
            "PYTEST_PLUGINS": "agentplane.system_tests.k3s.system_test_target",
        } | ({
            "K3S_RUNNER_LAYOUT_RLOCATION": "$(rlocationpath " + runner_layout + ")",
        } if runner_layout != None else {}) | env,
        exec_properties = K3S_EXEC_PROPERTIES | exec_properties,
        tags = K3S_TAGS + tags,
        main_module = main_module,
        deps = deps + ["//agentplane/system_tests/k3s:system_test_target"],
    )
