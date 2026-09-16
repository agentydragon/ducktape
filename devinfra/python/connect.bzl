"""Connect RPC service stubs over a standard `py_proto_library`.

Connecpy's generator ships as a compiled binary in its wheel's `.data/scripts`, with no console-script
entry point, so it is reached through the wheel's `:data` filegroup rather than
`py_console_script_binary` the way the mypy-protobuf plugins are. Messages stay with
`//devinfra/python:proto.bzl`, which owns the proto import graph; this rule generates only the
`<stem>_connecpy.py` service module the standard protobuf rules do not.
"""

load("@protobuf//bazel/common:proto_info.bzl", "ProtoInfo")
load("//devinfra/python:defs.bzl", "py_library")

_PLUGIN_NAME = "protoc-gen-connecpy"

def _connecpy_codegen_impl(ctx):
    proto_info = ctx.attr.proto[ProtoInfo]
    sources = proto_info.direct_sources
    if len(sources) != 1:
        fail("Expected exactly one direct proto source", "proto")

    plugins = [file for file in ctx.files._plugin if file.basename == _PLUGIN_NAME]
    if len(plugins) != 1:
        fail("Expected exactly one {} in the wheel's data scripts, got {}".format(_PLUGIN_NAME, plugins))
    plugin = plugins[0]

    # The plugin receives the standard provider's whole import closure and generates code only for
    # the direct schema, the same contract the gRPC service generator is driven under.
    ctx.actions.run(
        executable = ctx.executable._protoc,
        arguments = [
            "-I.",
            "--plugin={}={}".format(_PLUGIN_NAME, plugin.path),
            "--connecpy_out={}".format(ctx.bin_dir.path),
            sources[0].path,
        ],
        inputs = depset([plugin], transitive = [proto_info.transitive_sources]),
        outputs = [ctx.outputs.py],
        mnemonic = "ConnecpyServiceGen",
        use_default_shell_env = True,
    )
    return DefaultInfo(files = depset([ctx.outputs.py]))

_connecpy_codegen = rule(
    implementation = _connecpy_codegen_impl,
    attrs = {
        "module_name": attr.string(mandatory = True),
        "proto": attr.label(mandatory = True, providers = [ProtoInfo]),
        "_plugin": attr.label(default = Label("@pypi//protoc_gen_connecpy:data")),
        "_protoc": attr.label(default = Label("//devinfra/python:protoc"), executable = True, cfg = "exec"),
    },
    outputs = {"py": "%{module_name}.py"},
)

def py_connect_service_library(name, proto, py_proto, visibility = None):
    """A `<name>` Connect service library, where `<name>` is the proto stem plus `_connecpy`.

    `proto` is a standard `proto_library` whose `ProtoInfo` supplies the import closure, and
    `py_proto` owns the corresponding message modules.
    """
    codegen_name = name + "_codegen"
    _connecpy_codegen(
        name = codegen_name,
        module_name = name,
        proto = proto,
        visibility = ["//visibility:private"],
    )

    # Generated code, not ours to lint.
    py_library(
        name = name,
        srcs = [name + ".py"],
        tags = ["no-lint", "no-mypy"],
        visibility = visibility,
        deps = [
            py_proto,
            "@pypi//connecpy",
        ],
        pyi_deps = [py_proto],
    )
