"""Python rule wrappers that auto-inject repo-root imports."""

load("@rules_python//python:defs.bzl", _py_binary = "py_binary", _py_library = "py_library", _py_test = "py_test")

def repo_imports():
    """Compute imports path to the repository root from the current package.

    Returns:
        A single-element list with the relative path to the repo root.
    """
    pkg = native.package_name()
    if not pkg:
        return ["."]
    depth = pkg.count("/") + 1
    return ["/".join([".."] * depth)]

def py_library(imports = None, **kwargs):
    """py_library with auto repo-root imports."""
    if imports == None:
        imports = repo_imports()
    _py_library(imports = imports, **kwargs)

def py_binary(imports = None, **kwargs):
    """py_binary with auto repo-root imports."""
    if imports == None:
        imports = repo_imports()
    _py_binary(imports = imports, **kwargs)

def py_test(name, size = "small", requires_docker = False, uses_syrupy = False, tags = None, imports = None, args = None, deps = None, env_inherit = None, **kwargs):
    """py_test with auto repo-root imports and sensible defaults.

    Args:
        name: Target name.
        size: Test size. Defaults to 'small' (60s timeout). A target that times out on RBE
            without doing 60s of work is hitting executor I/O latency rather than its own
            cost -- size that target 'medium' where it happens, and see
            debug/2026_08_rbe_small_test_timeouts.md before assuming the test is at fault.
        requires_docker: Whether this test needs Docker. If True, adds the
            "requires_docker" tag and env_inherit for Docker TLS vars.
        uses_syrupy: Whether this test uses syrupy snapshots. If True, wires
            BazelAmberExtension to copy updated .ambr files to undeclared outputs.
        tags: Additional tags. Must not include "requires_docker" (use the parameter).
        imports: Python import paths. Defaults to repo root.
        args: Extra args passed to the test binary.
        deps: Test dependencies.
        env_inherit: Extra env vars to inherit. Docker vars are added automatically
            when requires_docker is True.
        **kwargs: Passed through to py_test.
    """
    if imports == None:
        imports = repo_imports()

    base_tags = tags or []
    if "requires_docker" in base_tags:
        fail("Use requires_docker parameter instead of 'requires_docker' tag in {}".format(name))

    base_env_inherit = list(env_inherit or [])
    base_args = list(args or [])
    base_deps = list(deps or [])
    if requires_docker:
        base_tags = base_tags + ["requires_docker"]

        # docker_mtls is currently a dormant no-op (external-RBE docker-ci access
        # is not wired up); see util/testing/docker_mtls.py. Kept loaded so the
        # hook is in place if that path is revived.
        base_args = base_args + ["-p", "util.testing.docker_mtls"]
        base_deps = base_deps + ["//util/testing:docker_mtls"]
    if uses_syrupy:
        base_args = base_args + [
            "--snapshot-default-extension=util.testing.bazel_snapshot_extension.BazelAmberExtension",
            "-p",
            "util.testing.bazel_snapshot_extension",
        ]
        base_deps = base_deps + [
            "//util/testing:bazel_snapshot_extension",
        ]

    _py_test(
        name = name,
        size = size,
        tags = base_tags,
        imports = imports,
        args = base_args,
        deps = base_deps,
        env_inherit = base_env_inherit if base_env_inherit else None,
        **kwargs
    )
