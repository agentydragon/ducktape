"""Python protobuf and gRPC modules from one `.proto`, with the stubs mypy reads.

The generator is the grpc project's own protoc and gRPC plugin as shipped in the `grpcio-tools`
wheel, so the gencode matches the `protobuf` and `grpcio` runtimes in the lockfile by
construction; `mypy-protobuf` adds the `.pyi` for messages and for the servicer and stub.

Deviation from the Bazel `grpc` module's `py_grpc_library`: that rule builds grpc's C++ core to
obtain the same plugin, and grpc 1.73 through 1.76 do not compile on the RBE worker toolchain
(missing standard headers under its gcc, and 1.75 pins a Python 3.14 beta rules_python cannot
resolve), so the wheel is the generator here.
"""

load("//devinfra/python:defs.bzl", "py_library")

_TOOLS = "//devinfra/python:protoc"
_MYPY_PLUGIN = "//devinfra/python:protoc_gen_mypy"
_MYPY_GRPC_PLUGIN = "//devinfra/python:protoc_gen_mypy_grpc"

def py_grpc_library(name, proto, visibility = None):
    """`<name>_pb2` and `<name>_pb2_grpc` libraries for `proto`, typed for mypy.

    Consumers import them as `<package>.<name>_pb2`; gazelle needs a `# gazelle:resolve py` directive
    for each, since no source file backs them. The proto may import only the well-known types bundled
    with protoc: no other `.proto` is on its include path.

    Args:
      name: the module stem, normally the proto's own stem.
      proto: the `.proto` file in this package.
      visibility: visibility of both libraries.
    """
    native.genrule(
        name = name + "_codegen",
        srcs = [proto],
        outs = [
            name + "_pb2.py",
            name + "_pb2.pyi",
            name + "_pb2_grpc.py",
            name + "_pb2_grpc.pyi",
        ],
        cmd = " ".join([
            "$(execpath %s)" % _TOOLS,
            "-I.",
            "--plugin=protoc-gen-mypy=$(execpath %s)" % _MYPY_PLUGIN,
            "--plugin=protoc-gen-mypy_grpc=$(execpath %s)" % _MYPY_GRPC_PLUGIN,
            "--python_out=$(BINDIR)",
            "--mypy_out=$(BINDIR)",
            "--grpc_python_out=$(BINDIR)",
            "--mypy_grpc_out=$(BINDIR)",
            "$(execpath %s)" % proto,
        ]),
        tools = [_TOOLS, _MYPY_PLUGIN, _MYPY_GRPC_PLUGIN],
    )

    # Generated code, not ours to lint.
    generated_tags = ["no-lint", "no-mypy"]
    py_library(
        name = name + "_pb2",
        srcs = [name + "_pb2.py"],
        pyi_srcs = [name + "_pb2.pyi"],
        tags = generated_tags,
        visibility = visibility,
        deps = ["@pypi//protobuf"],
    )
    py_library(
        name = name + "_pb2_grpc",
        srcs = [name + "_pb2_grpc.py"],
        pyi_srcs = [name + "_pb2_grpc.pyi"],
        tags = generated_tags,
        visibility = visibility,
        deps = [
            ":" + name + "_pb2",
            "@pypi//grpcio",
        ],
    )
