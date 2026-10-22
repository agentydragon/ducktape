"""Protobuf-ES generation over a proto_library's imported schema closure."""

load("@protobuf//bazel/common:proto_common.bzl", "proto_common")
load("@protobuf//bazel/common:proto_info.bzl", "ProtoInfo")
load("@rules_proto_grpc//:defs.bzl", "ProtoPluginInfo", "proto_compile_toolchains")

def _typescript_proto_compile_impl(ctx):
    # Deviation: rules_proto_grpc's generic compiler generates direct sources only. The browser
    # bundle owns modules for each imported schema, while Protobuf-ES owns the well-known types.
    proto_infos = [proto[ProtoInfo] for proto in ctx.attr.protos]
    source_closure = depset(transitive = [proto.transitive_sources for proto in proto_infos])
    import_roots = depset(transitive = [proto.transitive_proto_path for proto in proto_infos])
    descriptor_sets = depset(transitive = [proto.transitive_descriptor_sets for proto in proto_infos])

    sources_by_import_path = {}
    for source in source_closure.to_list():
        import_path = proto_common.get_import_path(source)
        if import_path.startswith("google/protobuf/"):
            continue
        if import_path.startswith("../") or import_path.startswith("/"):
            fail("Unsupported TypeScript proto import path: {}".format(import_path))
        if import_path.endswith(".proto"):
            protopath = import_path[:-len(".proto")]
        elif import_path.endswith(".protodevel"):
            protopath = import_path[:-len(".protodevel")]
        else:
            fail("Unsupported TypeScript proto import path: {}".format(import_path))
        previous = sources_by_import_path.get(import_path)
        if previous and previous.source != source:
            fail("Multiple proto sources have the same import path: {}".format(import_path))
        sources_by_import_path[import_path] = struct(source = source, protopath = protopath)

    if len(ctx.attr._plugins) != 1:
        fail("typescript_proto_compile requires exactly one rules_proto_grpc plugin")
    plugin = ctx.attr._plugins[0][ProtoPluginInfo]
    if plugin.out or plugin.output_directory or plugin.empty_template or not plugin.outputs:
        fail("typescript_proto_compile requires per-proto file outputs")
    plugin_tool = plugin.tool_provider.files_to_run
    plugin_executable = plugin_tool.executable
    proto_toolchain = ctx.toolchains[Label("@protobuf//bazel/private:proto_toolchain_type")]
    protoc = proto_toolchain.proto.proto_compiler.executable

    outputs = []
    output_root = "/".join([ctx.bin_dir.path, ctx.label.package, ctx.label.name])
    for import_path in sorted(sources_by_import_path.keys()):
        protopath = sources_by_import_path[import_path].protopath
        for pattern in plugin.outputs:
            output = ctx.actions.declare_file("{}/{}".format(
                ctx.label.name,
                pattern.replace("{protopath}", protopath),
            ))
            outputs.append(output)

    args = ctx.actions.args()
    args.set_param_file_format("multiline")
    args.use_param_file("@%s", use_always = False)
    args.add("--proto_path=.")
    args.add_all(["--proto_path=" + path for path in sorted(import_roots.to_list()) if path != "."])
    if descriptor_sets:
        args.add("--descriptor_set_in=" + ":".join(sorted([file.path for file in descriptor_sets.to_list()])))
    args.add("--plugin=protoc-gen-{}={}".format(plugin.protoc_plugin_name, plugin_executable.path))
    args.add("--{}_out={}:{}".format(plugin.protoc_plugin_name, ",".join(plugin.options), output_root))
    args.add_all(sorted(sources_by_import_path.keys()))

    ctx.actions.run(
        mnemonic = "TypescriptProtoCompile",
        executable = protoc,
        arguments = [args],
        inputs = depset(
            transitive = [source_closure, descriptor_sets, depset(plugin.data)],
        ),
        tools = [plugin_tool],
        outputs = outputs,
        env = {"BAZEL_BINDIR": ctx.bin_dir.path},
        use_default_shell_env = plugin.use_built_in_shell_environment,
        progress_message = "Generating TypeScript protos for {}".format(ctx.label),
        toolchain = Label("@protobuf//bazel/private:proto_toolchain_type"),
    )

    return DefaultInfo(files = depset(outputs))

typescript_proto_compile = rule(
    implementation = _typescript_proto_compile_impl,
    attrs = {
        "protos": attr.label_list(
            mandatory = True,
            providers = [ProtoInfo],
            doc = "Top-level proto schemas whose complete imported closure should be generated.",
        ),
        "_plugins": attr.label_list(
            providers = [ProtoPluginInfo],
            default = [Label("//agentplane/proto:protoc_gen_es_plugin")],
            cfg = "exec",
        ),
    },
    toolchains = proto_compile_toolchains,
)
