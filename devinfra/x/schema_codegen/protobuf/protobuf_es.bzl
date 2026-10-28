"""Spike-local Protobuf-ES conversion over a proto_library import closure."""

load("@protobuf//bazel/common:proto_common.bzl", "proto_common")
load("@protobuf//bazel/common:proto_info.bzl", "ProtoInfo")

def _protobuf_es_impl(ctx):
    proto_info = ctx.attr.proto[ProtoInfo]
    source_closure = proto_info.transitive_sources
    import_roots = proto_info.transitive_proto_path
    descriptor_sets = proto_info.transitive_descriptor_sets

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
        if previous and previous != source:
            fail("Multiple proto sources have the same import path: {}".format(import_path))
        sources_by_import_path[import_path] = struct(source = source, protopath = protopath)

    plugin = ctx.attr._protoc_gen_es[DefaultInfo].files_to_run
    proto_toolchain = ctx.toolchains[Label("@protobuf//bazel/private:proto_toolchain_type")]
    protoc = proto_toolchain.proto.proto_compiler.executable
    output_root = "/".join([ctx.bin_dir.path, ctx.label.package, ctx.label.name])

    outputs = []
    for import_path in sorted(sources_by_import_path.keys()):
        protopath = sources_by_import_path[import_path].protopath
        outputs.append(ctx.actions.declare_file("{}/{}_pb.js".format(ctx.label.name, protopath)))
        outputs.append(ctx.actions.declare_file("{}/{}_pb.d.ts".format(ctx.label.name, protopath)))

    args = ctx.actions.args()
    args.set_param_file_format("multiline")
    args.use_param_file("@%s", use_always = False)
    args.add("--proto_path=.")
    args.add_all(["--proto_path=" + path for path in sorted(import_roots.to_list()) if path != "."])
    if descriptor_sets:
        args.add("--descriptor_set_in=" + ":".join(sorted([file.path for file in descriptor_sets.to_list()])))
    args.add("--plugin=protoc-gen-es={}".format(plugin.executable.path))
    args.add("--es_out={}:{}".format(
        ",".join(["target=js+dts", "import_extension=js", "keep_empty_files=true"] + ctx.attr.options),
        output_root,
    ))
    args.add_all(sorted(sources_by_import_path.keys()))

    ctx.actions.run(
        mnemonic = "ProtobufEsCompile",
        executable = protoc,
        arguments = [args],
        inputs = depset(transitive = [source_closure, descriptor_sets]),
        tools = [plugin],
        outputs = outputs,
        env = {"BAZEL_BINDIR": ctx.bin_dir.path},
        use_default_shell_env = True,
        progress_message = "Generating Protobuf-ES import closure for {}".format(ctx.label),
        toolchain = Label("@protobuf//bazel/private:proto_toolchain_type"),
    )
    return DefaultInfo(files = depset(outputs))

protobuf_es = rule(
    implementation = _protobuf_es_impl,
    attrs = {
        "options": attr.string_list(
            doc = "Additional protoc-gen-es options, such as json_types=true.",
        ),
        "proto": attr.label(
            mandatory = True,
            providers = [ProtoInfo],
            doc = "The proto_library whose non-WKT imported schema closure should be generated.",
        ),
        "_protoc_gen_es": attr.label(
            default = Label(":protoc_gen_es"),
            executable = True,
            cfg = "exec",
        ),
    },
    toolchains = [Label("@protobuf//bazel/private:proto_toolchain_type")],
)
