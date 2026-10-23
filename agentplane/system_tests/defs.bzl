"""A system test scenario: run on a k3s stack of its own and, opted in, with a real model or live against a deployed instance."""

load("//agentplane/x/k3s_env:defs.bzl", "K3S_BINARY", "K3S_BINARY_RLOCATION", "K3S_EXEC_PROPERTIES", "K3S_TAGS", "K3S_VM_DATA", "K3S_VM_ENV")
load("//devinfra/python:defs.bzl", "py_library", "py_test")

_HARNESSES_LAYOUT = "//agentplane/x/k3s_env:harnesses/runner_layout"

# The k3s target's runner image, by what its scenarios run.
_RUNNER_LAYOUTS = {
    # No harness, so the image imports faster.
    "model_free": "//agentplane/x/k3s_env:lifecycle/runner_layout",
    "scripted": _HARNESSES_LAYOUT,
}

# The stack's manifest, LLM ingress forwarding to the scenario's scripted model; and with Dex and the
# MCP servers `test_mcp` calls, whose image layouts the stack then imports too.
_FIXTURE = "//agentplane/x/k3s_env:rendered/local-model.json"
_MCP_FIXTURE = "//agentplane/x/k3s_env:rendered/local-model-mcp-fixtures.json"
_MCP_FIXTURE_LAYOUTS = [
    "//agentplane/x/k3s_env:mcp_fixtures/dex_layout",
    "//agentplane/x/k3s_env:mcp_fixtures/mcp_everything_layout",
    "//agentplane/x/k3s_env:mcp_fixtures/oauth_fixture_layout",
]

# The `_vm` target's manifest: the local model's, with VM Sandboxes.
_VM_FIXTURE = "//agentplane/x/k3s_env:rendered/local-model-vm.json"

# The `_real_model` target's manifest: LLM ingress forwarding to the public LiteLLM with BuildBuddy's
# live-test key (`OPENAI_API_KEY`), decrypted into the test action alone, as for `live_openai_py_test`.
_REAL_MODEL_FIXTURE = "//agentplane/x/k3s_env:rendered/public-litellm.json"
_REAL_MODEL_SECRET = "OPENAI_API_KEY"

def system_test(name, srcs, deps, k3s = None, vm_sandboxes = None, real_model = False, live = True, mcp_fixtures = False, size = "small", timeout = None):
    """Generates `name` when `k3s` names its scenarios' kind, `name + "_vm"` when `vm_sandboxes` says
    where it runs, `name + "_real_model"` when `real_model` is True, and `name + "_live"` unless `live`
    is False.

    `name` starts its own k3s stack in one RBE action (`agentplane.x.k3s_env.system_test_target`) and
    runs the `scripted_model` scenarios and the unmarked ones, in PR CI like any test; `k3s` is
    `"model_free"` when none runs a turn, else `"scripted"`; `mcp_fixtures` adds Dex and the MCP
    servers to its stack. `name + "_vm"` does the same for the `vm_sandboxes` scenarios, which only it
    runs, on a stack with KubeVirt; `vm_sandboxes` is `"ci"` for what only a VM exercises (bring-up,
    networking), else `"manual"`, run by hand. `name + "_real_model"` runs the `real_model` scenarios on
    a k3s stack whose LLM ingress forwards to the public LiteLLM; it is tagged `live_openai_api`, so PR CI
    builds it and `--config=live-openai` runs it. `name + "_live"` runs against the deployment the caller's
    environment names (`AGENTPLANE_SYSTEM_TEST_*`, testing by default) with the caller's kubeconfig and
    a real model, off RBE; a scenario with only `scripted_model` cases has nothing to run there and
    passes `live = False`. `size` and `timeout` apply to `name + "_live"`.

    A hidden library owns the source, so all targets share one compiled copy. Gazelle does not
    manage macro rules: exclude the source in the BUILD file and keep `deps` by hand.
    """
    py_library(
        name = name + "_lib",
        testonly = True,
        srcs = srcs,
        deps = deps,
    )
    common = {
        "main_module": "pytest_bazel",
        "deps": [":" + name + "_lib", "@pypi//pytest_bazel"],
    }
    if live:
        py_test(
            name = name + "_live",
            size = size,
            timeout = timeout,
            # kubectl reads the caller's own kubeconfig; without HOME or KUBECONFIG it falls back to
            # http://localhost:8080 and fails for a reason that looks nothing like the cause.
            env_inherit = [
                "AGENTPLANE_SYSTEM_TEST_NAMESPACE",
                "AGENTPLANE_SYSTEM_TEST_SERVICE_ACCOUNT",
                "AGENTPLANE_SYSTEM_TEST_TOKEN",
                "AGENTPLANE_SYSTEM_TEST_URL",
                "HOME",
                "KUBECONFIG",
            ],
            tags = [
                "manual",
                "no-remote-exec",
            ],
            **common
        )
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
        _k3s_test(
            name = name + "_vm",
            # The guest's runner comes from the runner disk; no Sandbox runs the Pod runner image.
            runner_layout = _RUNNER_LAYOUTS["model_free"],
            fixture = _VM_FIXTURE,
            layouts = [],
            data = K3S_VM_DATA,
            env = K3S_VM_ENV,
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
            runner_layout,
            fixture,
            K3S_BINARY,
        ] + layouts + data,
        env = {
            "K3S_BINARY_RLOCATION": K3S_BINARY_RLOCATION,
            "AGENTPLANE_SYSTEM_TEST_MODEL_BACKEND": "scripted_model",
            "K3S_FIXTURE_RLOCATION": "$(rlocationpath " + fixture + ")",
            "K3S_MCP_FIXTURE_LAYOUTS": " ".join(["$(rlocationpath " + layout + ")" for layout in layouts]),
            "K3S_RUNNER_LAYOUT_RLOCATION": "$(rlocationpath " + runner_layout + ")",
            "PYTEST_PLUGINS": "agentplane.x.k3s_env.system_test_target",
        } | env,
        exec_properties = K3S_EXEC_PROPERTIES | exec_properties,
        tags = K3S_TAGS + tags,
        main_module = main_module,
        deps = deps + ["//agentplane/x/k3s_env:system_test_target"],
    )
