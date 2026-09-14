"""Typed Python service stubs where the available standard protobuf rules stop.

Messages use `@protobuf//bazel:py_proto_library`, which owns the proto import graph and generated
message modules. The workspace has no compatible `grpc` Bazel module: grpc 1.76 cannot load against
our protobuf 36 and newer grpc forces rules_python 2, which breaks the repository's rules_conda
extension. This narrow rule therefore only generates the gRPC service module with the plugins from
the pinned grpcio-tools and mypy-protobuf wheels.
"""

load("@protobuf//bazel/common:proto_info.bzl", "ProtoInfo")
load("//devinfra/python:defs.bzl", "py_library")

_TOOLS = "//devinfra/python:protoc"
_MYPY_GRPC_PLUGIN = "//devinfra/python:protoc_gen_mypy_grpc"

def _py_grpc_service_codegen_impl(ctx):
    proto_info = ctx.attr.proto[ProtoInfo]
    sources = proto_info.direct_sources
    if len(sources) != 1:
        fail("Expected exactly one direct proto source", "proto")

    source = sources[0]

    # This mirrors gRPC's own Bazel rules: the service generator receives the standard proto
    # provider's import closure, while it generates code only for the direct service schema.
    ctx.actions.run_shell(
        command = " ".join([
            ctx.executable._protoc.path,
            "-I.",
            "--plugin=protoc-gen-mypy_grpc={}".format(ctx.executable._mypy_grpc_plugin.path),
            "--grpc_python_out={}".format(ctx.bin_dir.path),
            "--mypy_grpc_out={}".format(ctx.bin_dir.path),
            source.path,
        ]),
        inputs = proto_info.transitive_sources,
        tools = [
            ctx.executable._protoc,
            ctx.executable._mypy_grpc_plugin,
        ],
        outputs = [
            ctx.outputs.py,
            ctx.outputs.pyi,
        ],
        mnemonic = "PyGrpcServiceGen",
        use_default_shell_env = True,
    )

    return DefaultInfo(files = depset([
        ctx.outputs.py,
        ctx.outputs.pyi,
    ]))

_py_grpc_service_codegen = rule(
    implementation = _py_grpc_service_codegen_impl,
    attrs = {
        "module_name": attr.string(mandatory = True),
        "proto": attr.label(
            mandatory = True,
            providers = [ProtoInfo],
        ),
        "_mypy_grpc_plugin": attr.label(
            default = Label(_MYPY_GRPC_PLUGIN),
            executable = True,
            cfg = "exec",
        ),
        "_protoc": attr.label(
            default = Label(_TOOLS),
            executable = True,
            cfg = "exec",
        ),
    },
    outputs = {
        "py": "%{module_name}.py",
        "pyi": "%{module_name}.pyi",
    },
)

def py_grpc_service_library(name, proto, py_proto, visibility = None):
    """A typed `<name>` gRPC service library over a standard `py_proto_library`.

    `proto` is a standard `proto_library`, whose `ProtoInfo` supplies the complete import closure;
    callers never repeat imported sources. `py_proto` owns the corresponding message modules. This
    rule deliberately owns only the service module that standard protobuf rules do not generate.
    """
    codegen_name = name + "_codegen"
    _py_grpc_service_codegen(
        name = codegen_name,
        module_name = name,
        proto = proto,
        visibility = ["//visibility:private"],
    )

    # Generated code, not ours to lint.
    py_library(
        name = name,
        srcs = [name + ".py"],
        pyi_srcs = [name + ".pyi"],
        tags = ["no-lint", "no-mypy"],
        visibility = visibility,
        deps = [
            py_proto,
            "@pypi//grpcio",
        ],
        pyi_deps = [py_proto],
    )
