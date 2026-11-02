"""Generate Protobuf-ES JavaScript for a proto_library's imported schema closure.

The pinned Aspect proto aspect leaves Google API imports such as
`../../google/api/annotations_pb.js` relative to the importing module while it emits
the imported module under its external-repository output path. The frontend's standard
esbuild bundle cannot resolve that layout, so this rule emits the complete closure into
one root where those relative imports stay valid. The runtime test checks direct Node
consumption of those outputs.
"""

load("@protobuf//bazel/common:proto_common.bzl", "proto_common")
load("@protobuf//bazel/common:proto_info.bzl", "ProtoInfo")

def _typescript_proto_compile_impl(ctx):
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

    protoc_gen_es = ctx.attr._protoc_gen_es[DefaultInfo].files_to_run
    protoc_gen_es_executable = protoc_gen_es.executable
    proto_toolchain = ctx.toolchains[Label("@protobuf//bazel/private:proto_toolchain_type")]
    protoc = proto_toolchain.proto.proto_compiler.executable

    outputs = []
    output_root = "/".join([ctx.bin_dir.path, ctx.label.package, ctx.label.name])
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
    args.add("--plugin=protoc-gen-es={}".format(protoc_gen_es_executable.path))
    plugin_options = ["target=js+dts", "import_extension=js", "keep_empty_files=true"] + ctx.attr.options
    args.add("--es_out={}:{}".format(",".join(plugin_options), output_root))
    args.add_all(sorted(sources_by_import_path.keys()))

    ctx.actions.run(
        mnemonic = "TypescriptProtoCompile",
        executable = protoc,
        arguments = [args],
        inputs = depset(
            transitive = [source_closure, descriptor_sets],
        ),
        tools = [protoc_gen_es],
        outputs = outputs,
        env = {"BAZEL_BINDIR": ctx.bin_dir.path},
        use_default_shell_env = True,
        progress_message = "Generating TypeScript protos for {}".format(ctx.label),
        toolchain = Label("@protobuf//bazel/private:proto_toolchain_type"),
    )

    return DefaultInfo(files = depset(outputs))

typescript_proto_compile = rule(
    implementation = _typescript_proto_compile_impl,
    attrs = {
        "options": attr.string_list(
            doc = "Additional protoc-gen-es options, such as json_types=true.",
        ),
        "protos": attr.label_list(
            mandatory = True,
            providers = [ProtoInfo],
            doc = "Top-level proto schemas whose complete imported closure should be generated.",
        ),
        "_protoc_gen_es": attr.label(
            default = Label("//agentplane/proto:protoc_gen_es"),
            executable = True,
            cfg = "exec",
        ),
    },
    toolchains = [Label("@protobuf//bazel/private:proto_toolchain_type")],
)
