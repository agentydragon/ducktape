"""Standard Python protobuf libraries with the stub closure visible to rules_mypy."""

load("@protobuf//bazel:py_proto_library.bzl", _py_proto_library = "py_proto_library")
load("@rules_python//python:defs.bzl", "PyInfo")

def _py_proto_with_mypy_stubs_impl(ctx):
    generated = ctx.attr.generated
    py_info = generated[PyInfo]
    runtime = [dep[PyInfo] for dep in ctx.attr.runtime_deps]

    # rules_mypy reads `direct_pyi_files` from each direct dependency. Standard py_proto_library
    # correctly records the full closure in `transitive_pyi_files`, but intentionally exposes only
    # the direct schema there. Presenting the closure here lets generated stubs import schemas the
    # direct proto imports, without taking over protobuf code generation or its proto graph.
    return [
        # Preserve every runtime/runfiles field from the standard generated target.  The adapter
        # changes only the shape through which rules_mypy reads its generated stubs, and adds
        # `runtime_deps`.
        DefaultInfo(
            files = generated[DefaultInfo].files,
            runfiles = generated[DefaultInfo].default_runfiles.merge_all(
                [dep[DefaultInfo].default_runfiles for dep in ctx.attr.runtime_deps],
            ),
        ),
        PyInfo(
            direct_pyi_files = py_info.transitive_pyi_files,
            has_py2_only_sources = py_info.has_py2_only_sources,
            has_py3_only_sources = py_info.has_py3_only_sources,
            imports = depset(transitive = [py_info.imports] + [info.imports for info in runtime]),
            transitive_pyi_files = py_info.transitive_pyi_files,
            transitive_sources = depset(transitive = [py_info.transitive_sources] + [info.transitive_sources for info in runtime]),
            uses_shared_libraries = py_info.uses_shared_libraries or any([info.uses_shared_libraries for info in runtime]),
        ),
    ]

_py_proto_with_mypy_stubs = rule(
    implementation = _py_proto_with_mypy_stubs_impl,
    attrs = {
        "generated": attr.label(
            mandatory = True,
            providers = [PyInfo],
        ),
        "runtime_deps": attr.label_list(providers = [PyInfo]),
    },
)

def py_proto_library_with_mypy_stubs(name, deps, runtime_deps = [], visibility = None):
    """Expose a standard `py_proto_library` and its transitive stubs to rules_mypy.

    `runtime_deps` are Python libraries the generated modules import at runtime that the
    generated closure does not carry.
    """
    generated_name = name + "_generated"
    _py_proto_library(
        name = generated_name,
        deps = deps,
        visibility = ["//visibility:private"],
    )
    _py_proto_with_mypy_stubs(
        name = name,
        generated = ":" + generated_name,
        runtime_deps = runtime_deps,
        visibility = visibility,
    )
