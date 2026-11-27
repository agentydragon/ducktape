"""Build Lean test executables and register those that run directly in Bazel."""

load("@rules_lean//lean:defs.bzl", "lean_binary", "lean_library")
load("@rules_shell//shell:sh_test.bzl", "sh_test")

def test_runner(name, src, deps, visibility = None):
    """Build a standalone Lean test executable for a test's runfiles."""
    modules = name + "_modules"
    lean_library(
        name = modules,
        testonly = True,
        srcs = [src],
        visibility = ["//visibility:private"],
        deps = deps,
    )
    lean_binary(
        name = name,
        testonly = True,
        visibility = visibility,
        deps = [":" + modules],
    )

def executable_test(name, src, deps):
    """Run a Lean test executable that needs no arguments or extra runfiles."""
    runner = name + "_runner"
    test_runner(name = runner, src = src, deps = deps)
    sh_test(
        name = name,
        srcs = ["checked_split_test.sh"],
        args = ["$(rlocationpath :%s)" % runner],
        data = [":" + runner],
        use_bash_launcher = True,
    )
