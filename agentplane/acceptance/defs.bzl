"""An acceptance scenario, run against a deployed instance and, opted in, a k3s stack of its own."""

load("//agentplane/x/k3s_env:defs.bzl", "K3S_BINARY", "K3S_BINARY_RLOCATION", "K3S_EXEC_PROPERTIES", "K3S_TAGS")
load("//devinfra/python:defs.bzl", "py_library", "py_test")

_RUNNER_LAYOUT = "//agentplane/x/k3s_env:lifecycle/runner_layout"

def acceptance_test(name, srcs, deps, k3s = False, size = "small", timeout = None):
    """Generates `name`, and `name + "_k3s"` when `k3s` is set.

    `name` runs against the deployment the caller's environment names (`AGENTPLANE_ACCEPTANCE_*`,
    testing by default) with the caller's kubeconfig, off RBE. `name + "_k3s"` starts its own stack
    in one RBE action (`agentplane.x.k3s_env.acceptance_target`); its runner image has no harness, so
    only model-free scenarios opt in. `size` and `timeout` apply to `name`.

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
    if not k3s:
        return
    py_test(
        name = name + "_k3s",
        size = "enormous",
        data = [
            _RUNNER_LAYOUT,
            "@nodejs_linux_amd64//:bin/node",
            K3S_BINARY,
        ],
        env = {
            "JSII_NODE": "$(rootpath @nodejs_linux_amd64//:bin/node)",
            "K3S_BINARY_RLOCATION": K3S_BINARY_RLOCATION,
            "K3S_RUNNER_LAYOUT_RLOCATION": "$(rlocationpath " + _RUNNER_LAYOUT + ")",
            # Loaded by name, not argv: the fixture chart parses argv as app settings.
            "PYTEST_PLUGINS": "agentplane.x.k3s_env.acceptance_target",
        },
        exec_properties = K3S_EXEC_PROPERTIES,
        tags = K3S_TAGS,
        main_module = common["main_module"],
        deps = common["deps"] + ["//agentplane/x/k3s_env:acceptance_target"],
    )
