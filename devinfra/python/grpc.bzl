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

def py_grpc_library(name, proto, imports = [], py_deps = [], visibility = None):
    """`<name>_pb2` and `<name>_pb2_grpc` libraries for `proto`, typed for mypy.

    Consumers import them as `<package>.<name>_pb2`; gazelle needs a `# gazelle:resolve py` directive
    for each, since no source file backs them. `imports` makes project-local proto sources visible to
    protoc; `py_deps` supplies their generated Python modules to this generated library.

    Args:
      name: the module stem, normally the proto's own stem.
      proto: the `.proto` file in this package.
      imports: project-local `.proto` labels this proto imports.
      py_deps: generated Python protobuf labels corresponding to `imports`, for both runtime and
        `.pyi` imports.
      visibility: visibility of both libraries.
    """
    native.genrule(
        name = name + "_codegen",
        srcs = [proto] + imports,
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
        # rules_mypy obtains generated dependency roots from default runfiles, whereas rules_python
        # deliberately excludes `pyi_srcs` from them. Retain this tiny generated stub as data so
        # imported protobuf message types remain visible to type checks of downstream libraries.
        data = [name + "_pb2.pyi"],
        tags = generated_tags,
        visibility = visibility,
        deps = ["@pypi//protobuf"] + py_deps,
        pyi_deps = py_deps,
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
