"""Typed Python service stubs where the available standard protobuf rules stop.

Messages use `@protobuf//bazel:py_proto_library`, which owns the proto import graph and generated
message stubs. The workspace has no compatible `grpc` Bazel module: grpc 1.76 cannot load against
our protobuf 36 and newer grpc forces rules_python 2, which breaks the repository's rules_conda
extension. This narrow rule therefore only generates the gRPC service module with the plugins from
the pinned grpcio-tools and mypy-protobuf wheels.
"""

load("//devinfra/python:defs.bzl", "py_library")

_TOOLS = "//devinfra/python:protoc"
_MYPY_GRPC_PLUGIN = "//devinfra/python:protoc_gen_mypy_grpc"

def py_grpc_service_library(name, proto, py_proto, visibility = None):
    """A typed `<name>` gRPC service library over a standard `py_proto_library`.

    The supplied `py_proto` owns all message modules and their imported message closure. This rule
    deliberately owns only the service module the standard protobuf rules do not generate.

    Args:
      name: the generated gRPC module stem, normally `<proto-stem>_pb2_grpc`.
      proto: the local `.proto` source declaring the service.
      py_proto: standard `py_proto_library` target for `proto`.
      visibility: visibility of the generated library.
    """
    native.genrule(
        name = name + "_codegen",
        srcs = [proto],
        outs = [
            name + ".py",
            name + ".pyi",
        ],
        cmd = " ".join([
            "$(execpath %s)" % _TOOLS,
            "-I.",
            "--plugin=protoc-gen-mypy_grpc=$(execpath %s)" % _MYPY_GRPC_PLUGIN,
            "--grpc_python_out=$(BINDIR)",
            "--mypy_grpc_out=$(BINDIR)",
            "$(execpath %s)" % proto,
        ]),
        tools = [_TOOLS, _MYPY_GRPC_PLUGIN],
    )

    # Generated code, not ours to lint.
    generated_tags = ["no-lint", "no-mypy"]
    py_library(
        name = name,
        srcs = [name + ".py"],
        pyi_srcs = [name + ".pyi"],
        tags = generated_tags,
        visibility = visibility,
        deps = [
            py_proto,
            "@pypi//grpcio",
        ],
        pyi_deps = [py_proto],
    )
