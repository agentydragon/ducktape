load("@rules_proto_grpc//:defs.bzl", "ProtoPluginInfo", "proto_compile_attrs", "proto_compile_impl", "proto_compile_toolchains")

typescript_proto_compile = rule(
    implementation = proto_compile_impl,
    attrs = dict(
        proto_compile_attrs,
        _plugins = attr.label_list(
            providers = [ProtoPluginInfo],
            default = [Label(":protoc_gen_es_plugin")],
            cfg = "exec",
        ),
    ),
    toolchains = proto_compile_toolchains,
)
