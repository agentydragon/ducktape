"""Standard Python protobuf libraries with the stub closure visible to rules_mypy."""

load("@protobuf//bazel:py_proto_library.bzl", _py_proto_library = "py_proto_library")
load("@protobuf//bazel/common:proto_common.bzl", "proto_common")
load("@protobuf//bazel/common:proto_info.bzl", "ProtoInfo")
load("@rules_python//python:defs.bzl", "PyInfo")

def _generate_mypy_stubs(ctx):
    sources = depset(transitive = [dep[ProtoInfo].transitive_sources for dep in ctx.attr.protos])
    paths = [proto_common.get_import_path(source) for source in sources.to_list()]
    stub_root = ctx.label.name + "_mypy"
    stubs = [
        ctx.actions.declare_file(stub_root + "/" + path.removesuffix(".proto").replace("-", "_").replace(".", "/") + "_pb2.pyi")
        for path in paths
    ]
    output_root = ctx.bin_dir.path + "/" + ctx.label.package + "/" + stub_root
    roots = depset(transitive = [dep[ProtoInfo].transitive_proto_path for dep in ctx.attr.protos])
    ctx.actions.run(
        executable = ctx.executable._protoc,
        arguments = [
            "--plugin=protoc-gen-mypy=" + ctx.executable._mypy_plugin.path,
            "--mypy_out=" + output_root,
        ] + ["-I" + root for root in roots.to_list()] + paths,
        inputs = sources,
        tools = [ctx.executable._mypy_plugin],
        outputs = stubs,
        mnemonic = "PyProtoMypyStub",
        use_default_shell_env = True,
    )

    # rules_mypy removes the repository component of imports and searches these
    # execroot paths before native generated modules. Runtime code stays with protoc.
    return depset(stubs), ctx.workspace_name + "/" + output_root

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
    stubs = py_info.transitive_pyi_files
    if ctx.attr.mypy_protobuf:
        stubs, stub_import = _generate_mypy_stubs(ctx)
        imports = depset([stub_import], transitive = [imports])

    return [
        # Preserve the generated target's runtime/runfiles files and add the external proto
        # import roots. The adapter also exposes the full stub closure to rules_mypy.
        generated[DefaultInfo],
        PyInfo(
            direct_pyi_files = stubs,
            has_py2_only_sources = py_info.has_py2_only_sources,
            has_py3_only_sources = py_info.has_py3_only_sources,
            imports = imports,
            transitive_pyi_files = stubs,
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
        "mypy_protobuf": attr.bool(),
        "protos": attr.label_list(providers = [ProtoInfo]),
        "_mypy_plugin": attr.label(default = Label("//devinfra/python:protoc_gen_mypy"), executable = True, cfg = "exec"),
        "_protoc": attr.label(default = Label("//devinfra/python:protoc"), executable = True, cfg = "exec"),
    },
)

def py_proto_library_with_mypy_stubs(name, deps, visibility = None, mypy_protobuf = False):
    """Expose standard protobuf runtime code and its stub closure to rules_mypy.

    `mypy_protobuf` replaces native `.pyi` files with the pinned plugin's output for
    the full import closure, including external annotation schemas.
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
        mypy_protobuf = mypy_protobuf,
        protos = deps if mypy_protobuf else [],
        visibility = visibility,
    )
