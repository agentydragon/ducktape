"""An acceptance scenario, run against a deployed instance and, opted in, a k3s stack of its own."""

load("//agentplane/x/k3s_env:defs.bzl", "K3S_BINARY", "K3S_BINARY_RLOCATION", "K3S_EXEC_PROPERTIES", "K3S_TAGS")
load("//devinfra/python:defs.bzl", "py_library", "py_test")

# The `_k3s` target's runner image, by what its scenarios run.
_RUNNER_LAYOUTS = {
    # No harness, so the image imports faster.
    "model_free": "//agentplane/x/k3s_env:lifecycle/runner_layout",
    "scripted": "//agentplane/x/k3s_env:harnesses/runner_layout",
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

def acceptance_test(name, srcs, deps, k3s = None, deployed = True, mcp_fixtures = False, size = "small", timeout = None):
    """Generates `name` unless `deployed` is False, and `name + "_k3s"` when `k3s` names its
    scenarios' kind.

    `name` runs against the deployment the caller's environment names (`AGENTPLANE_ACCEPTANCE_*`,
    testing by default) with the caller's kubeconfig and a real model, off RBE; a scenario with only
    `scripted_model` cases has nothing to run there and passes `deployed = False`. `name + "_k3s"`
    starts its own stack in one RBE action (`agentplane.x.k3s_env.acceptance_target`) and runs the
    `scripted_model` scenarios and the unmarked ones, in PR CI like any test; `k3s` is `"model_free"`
    when none runs a turn, else `"scripted"`; `mcp_fixtures` adds Dex and the MCP servers to its
    stack. `size` and `timeout` apply to `name`.

    A hidden library owns the source, so both targets share one compiled copy. Gazelle does not
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
    if deployed:
        py_test(
            name = name,
            size = size,
            timeout = timeout,
            # kubectl reads the caller's own kubeconfig; without HOME or KUBECONFIG it falls back to
            # http://localhost:8080 and fails for a reason that looks nothing like the cause.
            env_inherit = [
                "AGENTPLANE_ACCEPTANCE_NAMESPACE",
                "AGENTPLANE_ACCEPTANCE_SERVICE_ACCOUNT",
                "AGENTPLANE_ACCEPTANCE_TOKEN",
                "AGENTPLANE_ACCEPTANCE_URL",
                "HOME",
                "KUBECONFIG",
            ],
            tags = [
                "manual",
                "no-remote-exec",
            ],
            **common
        )
    if k3s == None:
        return
    runner_layout = _RUNNER_LAYOUTS[k3s]
    fixture = _MCP_FIXTURE if mcp_fixtures else _FIXTURE
    mcp_fixture_layouts = _MCP_FIXTURE_LAYOUTS if mcp_fixtures else []
    py_test(
        name = name + "_k3s",
        size = "enormous",
        data = [
            runner_layout,
            fixture,
            K3S_BINARY,
        ] + mcp_fixture_layouts,
        env = {
            "K3S_BINARY_RLOCATION": K3S_BINARY_RLOCATION,
            "AGENTPLANE_ACCEPTANCE_MODEL_BACKEND": "scripted_model",
            "K3S_FIXTURE_RLOCATION": "$(rlocationpath " + fixture + ")",
            "K3S_MCP_FIXTURE_LAYOUTS": " ".join(["$(rlocationpath " + layout + ")" for layout in mcp_fixture_layouts]),
            "K3S_RUNNER_LAYOUT_RLOCATION": "$(rlocationpath " + runner_layout + ")",
            "PYTEST_PLUGINS": "agentplane.x.k3s_env.acceptance_target",
        },
        exec_properties = K3S_EXEC_PROPERTIES,
        tags = K3S_TAGS,
        main_module = common["main_module"],
        deps = common["deps"] + ["//agentplane/x/k3s_env:acceptance_target"],
    )
