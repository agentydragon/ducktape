"""Adapters from the split-proof targets to the native Lean rules.

The upstream library compiles one source per action. These macros retain the
existing public binary and proof-test labels while giving their sources a
library target that the native linker and axiom audit can consume.
"""

load("@bazel_skylib//rules:build_test.bzl", "build_test")
load("@rules_lean//lean:defs.bzl", _lean_binary = "lean_binary", _lean_library = "lean_library", _lean_proof_test = "lean_proof_test")

lean_library = _lean_library

def lean_binary(name, srcs, entry, deps = [], **kwargs):
    if not entry:
        fail("lean_binary %s needs an entry" % name)
    lib = name + "_modules"
    _lean_library(
        name = lib,
        srcs = srcs,
        deps = deps,
        tags = kwargs.get("tags", []),
        testonly = kwargs.get("testonly", False),
        visibility = ["//visibility:private"],
    )
    _lean_binary(name = name, deps = [":" + lib], **kwargs)

def lean_test(name, srcs, entry, deps = [], **kwargs):
    if not entry:
        fail("lean_test %s needs an entry" % name)
    lib = name + "_modules"
    _lean_library(
        name = lib,
        srcs = srcs,
        deps = deps,
        tags = kwargs.get("tags", []),
        testonly = True,
        visibility = ["//visibility:private"],
    )
    build_test(name = name, targets = [":" + lib], **kwargs)

def lean_axiom_test(name, imports, theorems, srcs = [], deps = [], **kwargs):
    if not theorems:
        fail("lean_axiom_test %s needs named declarations" % name)
    audit_deps = deps
    if srcs:
        lib = name + "_modules"
        _lean_library(
            name = lib,
            srcs = srcs,
            deps = deps,
            tags = kwargs.get("tags", []),
            testonly = True,
            visibility = ["//visibility:private"],
        )
        audit_deps = [":" + lib]
    _lean_proof_test(
        name = name,
        deps = audit_deps,
        modules = imports,
        # Import-only root modules may contain no declarations of their own.
        # The nonempty named list is still checked, including imported names.
        allow_empty = True,
        required_theorems = theorems,
        **kwargs
    )

def _lean_source_copy_impl(ctx):
    out = ctx.actions.declare_file(ctx.attr.module_path)
    ctx.actions.symlink(output = out, target_file = ctx.file.src)
    return [DefaultInfo(files = depset([out]))]

lean_source_copy = rule(
    implementation = _lean_source_copy_impl,
    attrs = {
        "module_path": attr.string(mandatory = True),
        "src": attr.label(allow_single_file = [".lean"], mandatory = True),
    },
)

def _lean_compiler_impl(ctx):
    tc = ctx.toolchains["@rules_lean//lean:toolchain_type"]

    # Expose the actual SDK binary, so its relative shared-library and sysroot
    # paths are preserved when the test resolves it through runfiles.
    return [DefaultInfo(
        files = depset([tc.lean]),
        runfiles = ctx.runfiles(transitive_files = tc.lean_files),
    )]

lean_compiler = rule(
    implementation = _lean_compiler_impl,
    toolchains = ["@rules_lean//lean:toolchain_type"],
)
