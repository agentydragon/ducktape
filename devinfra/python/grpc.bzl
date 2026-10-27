"""Typed Python gRPC service libraries.

gRPC's own `py_grpc_library` owns the service module. It carries no `.pyi`, and no standard rule
generates one, so mypy would see the stub's methods as `Any`; the stub-only generator below fills
that gap with the pinned mypy-protobuf plugin. Messages come from `@protobuf//bazel:py_proto_library`
via `//devinfra/python:proto.bzl`, which owns the proto import graph.
"""

load("@grpc//bazel:python_rules.bzl", "py_grpc_library")
load("@protobuf//bazel/common:proto_info.bzl", "ProtoInfo")
load("@rules_python//python:defs.bzl", "PyInfo")
load("//devinfra/python:defs.bzl", "py_library")

_MYPY_GRPC_PLUGIN = "//devinfra/python:protoc_gen_mypy_grpc"

def _mypy_grpc_stub_impl(ctx):
    proto_info = ctx.attr.proto[ProtoInfo]
    sources = proto_info.direct_sources
    if len(sources) != 1:
        fail("Expected exactly one direct proto source", "proto")

    # The plugin receives the standard provider's import closure and generates only the direct
    # schema's stubs, mirroring how the service generator is driven. Concrete servicer stubs match
    # the runtime class, whose every RPC answers UNIMPLEMENTED, so a test fake overrides only the RPCs
    # it drives; abstract ones would force a hand-written all-UNIMPLEMENTED base per service.
    # External imports (googleapis' `google/api/annotations.proto`) resolve under their own source root.
    import_roots = ["-I" + root for root in proto_info.transitive_proto_path.to_list() if root != "."]
    parameters = ["generate_concrete_servicer_stubs"]
    if ctx.attr.async_only:
        parameters.append("only_async")
    ctx.actions.run(
        executable = ctx.executable._protoc,
        arguments = [
            "-I.",
            "--plugin=protoc-gen-mypy_grpc={}".format(ctx.executable._mypy_grpc_plugin.path),
            "--mypy_grpc_out={}:{}".format(",".join(parameters), ctx.bin_dir.path),
        ] + import_roots + [sources[0].path],
        inputs = proto_info.transitive_sources,
        tools = [ctx.executable._mypy_grpc_plugin],
        outputs = [ctx.outputs.pyi],
        mnemonic = "PyGrpcMypyStub",
        use_default_shell_env = True,
    )
    return DefaultInfo(files = depset([ctx.outputs.pyi]))

_mypy_grpc_stub = rule(
    implementation = _mypy_grpc_stub_impl,
    attrs = {
        "async_only": attr.bool(),
        "module_name": attr.string(mandatory = True),
        "proto": attr.label(mandatory = True, providers = [ProtoInfo]),
        "_mypy_grpc_plugin": attr.label(default = Label(_MYPY_GRPC_PLUGIN), executable = True, cfg = "exec"),
        "_protoc": attr.label(default = Label("//devinfra/python:protoc"), executable = True, cfg = "exec"),
    },
    outputs = {"pyi": "%{module_name}.pyi"},
)

def _message_stubs_impl(ctx):
    return DefaultInfo(files = ctx.attr.py_proto[PyInfo].direct_pyi_files)

# rules_mypy reads only each direct dependency's `direct_pyi_files`, so a module importing just the
# service module sees the message module as untyped `.py`. Re-exporting the message stubs as this
# library's own `pyi_srcs` puts them in front of mypy for every consumer.
_message_stubs = rule(
    implementation = _message_stubs_impl,
    attrs = {"py_proto": attr.label(mandatory = True, providers = [PyInfo])},
)

def py_grpc_service_library(name, proto, py_proto, visibility = None, async_only = False):
    """A typed `<name>` gRPC service library over a standard `py_proto_library`.

    `proto` is a standard `proto_library`, whose `ProtoInfo` supplies the complete import closure;
    callers never repeat imported sources. `py_proto` owns the corresponding message modules.
    `async_only` types stubs and servicers for grpc.aio channels/servers without sync overloads.
    """
    py_grpc_library(
        name = name + "_service",
        srcs = [proto],
        deps = [py_proto],
        grpc_library = "@pypi//grpcio",
        visibility = ["//visibility:private"],
    )
    _message_stubs(
        name = name + "_message_stubs",
        py_proto = py_proto,
        visibility = ["//visibility:private"],
    )
    _mypy_grpc_stub(
        name = name + "_stub",
        async_only = async_only,
        module_name = name,
        proto = proto,
        visibility = ["//visibility:private"],
    )

    # Generated code, not ours to lint.
    py_library(
        name = name,
        srcs = [],
        pyi_srcs = [":" + name + "_stub", ":" + name + "_message_stubs"],
        tags = ["no-lint", "no-mypy"],
        visibility = visibility,
        deps = [name + "_service", py_proto],
    )
