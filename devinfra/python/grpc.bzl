"""Typed Python gRPC service libraries.

gRPC's own `py_grpc_library` owns the service module. It carries no `.pyi`, and no standard rule
generates one, so mypy would see the stub's methods as `Any`; the stub-only generator below fills
that gap with the pinned mypy-protobuf plugin. Messages come from `@protobuf//bazel:py_proto_library`
via `//devinfra/python:proto.bzl`, which owns the proto import graph.
"""

load("@grpc//bazel:python_rules.bzl", "py_grpc_library")
load("@protobuf//bazel/common:proto_info.bzl", "ProtoInfo")
load("//devinfra/python:defs.bzl", "py_library")

_MYPY_GRPC_PLUGIN = "//devinfra/python:protoc_gen_mypy_grpc"

def _mypy_grpc_stub_impl(ctx):
    proto_info = ctx.attr.proto[ProtoInfo]
    sources = proto_info.direct_sources
    if len(sources) != 1:
        fail("Expected exactly one direct proto source", "proto")

    # The plugin receives the standard provider's import closure and generates only the direct
    # schema's stubs, mirroring how the service generator is driven.
    ctx.actions.run(
        executable = ctx.executable._protoc,
        arguments = [
            "-I.",
            "--plugin=protoc-gen-mypy_grpc={}".format(ctx.executable._mypy_grpc_plugin.path),
            "--mypy_grpc_out={}".format(ctx.bin_dir.path),
            sources[0].path,
        ],
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
        "module_name": attr.string(mandatory = True),
        "proto": attr.label(mandatory = True, providers = [ProtoInfo]),
        "_mypy_grpc_plugin": attr.label(default = Label(_MYPY_GRPC_PLUGIN), executable = True, cfg = "exec"),
        "_protoc": attr.label(default = Label("//devinfra/python:protoc"), executable = True, cfg = "exec"),
    },
    outputs = {"pyi": "%{module_name}.pyi"},
)

def py_grpc_service_library(name, proto, py_proto, visibility = None):
    """A typed `<name>` gRPC service library over a standard `py_proto_library`.

    `proto` is a standard `proto_library`, whose `ProtoInfo` supplies the complete import closure;
    callers never repeat imported sources. `py_proto` owns the corresponding message modules.
    """
    py_grpc_library(
        name = name + "_service",
        srcs = [proto],
        deps = [py_proto],
        grpc_library = "@pypi//grpcio",
        visibility = ["//visibility:private"],
    )
    _mypy_grpc_stub(
        name = name + "_stub",
        module_name = name,
        proto = proto,
        visibility = ["//visibility:private"],
    )

    # Generated code, not ours to lint.
    py_library(
        name = name,
        srcs = [],
        pyi_srcs = [":" + name + "_stub"],
        tags = ["no-lint", "no-mypy"],
        visibility = visibility,
        deps = [name + "_service"],
        pyi_deps = [py_proto],
    )
