"""Standard Python protobuf libraries with the stub closure visible to rules_mypy."""

load("@protobuf//bazel:py_proto_library.bzl", _py_proto_library = "py_proto_library")
load("@rules_python//python:defs.bzl", "PyInfo")

def _py_proto_with_mypy_stubs_impl(ctx):
    generated = ctx.attr.generated
    py_info = generated[PyInfo]

    # rules_mypy reads `direct_pyi_files` from each direct dependency. Standard py_proto_library
    # correctly records the full closure in `transitive_pyi_files`, but intentionally exposes only
    # the direct schema there. Presenting the closure here lets generated stubs import schemas the
    # direct proto imports, without taking over protobuf code generation or its proto graph.
    # Generated sources from external proto dependencies are runfiles under their external
    # repository root (`../repo/...` in File.short_path). Make each root importable: aspect_rules_py
    # builds the binary's .pth from PyInfo.imports, and the standard py_proto_library provider only
    # adds imports for virtual roots.
    external_import_roots = depset([
        source.short_path.split("/")[1]
        for source in py_info.transitive_sources.to_list()
        if source.short_path.startswith("../")
    ])
    imports = depset(transitive = [py_info.imports, external_import_roots])

    return [
        # Preserve the generated target's runtime/runfiles files and add the external proto
        # import roots. The adapter also exposes the full stub closure to rules_mypy.
        generated[DefaultInfo],
        PyInfo(
            direct_pyi_files = py_info.transitive_pyi_files,
            has_py2_only_sources = py_info.has_py2_only_sources,
            has_py3_only_sources = py_info.has_py3_only_sources,
            imports = imports,
            transitive_pyi_files = py_info.transitive_pyi_files,
            transitive_sources = py_info.transitive_sources,
            uses_shared_libraries = py_info.uses_shared_libraries,
        ),
    ]

_py_proto_with_mypy_stubs = rule(
    implementation = _py_proto_with_mypy_stubs_impl,
    attrs = {
        "generated": attr.label(
            mandatory = True,
            providers = [PyInfo],
        ),
    },
)

def py_proto_library_with_mypy_stubs(name, deps, visibility = None):
    """Expose a standard `py_proto_library` and its transitive stubs to rules_mypy."""
    generated_name = name + "_generated"
    _py_proto_library(
        name = generated_name,
        deps = deps,
        visibility = ["//visibility:private"],
    )
    _py_proto_with_mypy_stubs(
        name = name,
        generated = ":" + generated_name,
        visibility = visibility,
    )
