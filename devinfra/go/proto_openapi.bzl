"""OpenAPI document of a `proto_library`'s `google.api.http` rules.

The document is protoc-gen-openapiv2's (grpc-gateway), so it describes the routes `//devinfra/go:grpc_gateway`
generates and takes each description from the proto comment beside it. `protoc` is the pinned wheel of
//devinfra/python:protoc, so no second `protoc` is built.
"""

load("@protobuf//bazel/common:proto_info.bzl", "ProtoInfo")

def _proto_openapi_impl(ctx):
    proto_info = ctx.attr.proto[ProtoInfo]
    sources = proto_info.direct_sources
    if len(sources) != 1:
        fail("Expected exactly one direct proto source", "proto")

    # The plugin names a merged document `<merge_file_name>.swagger.json` under the output root, so the
    # merge name is this rule's own path and the declared output is where the plugin writes it.
    document = ctx.actions.declare_file(ctx.label.name + ".swagger.json")
    merge_file_name = "{}/{}".format(ctx.label.package, ctx.label.name)

    # External imports (googleapis' `google/api/annotations.proto`) resolve under their own source root.
    import_roots = ["-I" + root for root in proto_info.transitive_proto_path.to_list() if root != "."]

    # The plugin insists on a Go import path for every file it reads, though none reaches the document;
    # `go_proto_library` supplies the real one to the Go compilers, so a proto here carries no `go_package`.
    go_import_path = "M{}={}".format(sources[0].path, sources[0].dirname)
    ctx.actions.run(
        executable = ctx.executable._protoc,
        arguments = [
            "-I.",
            "--plugin=protoc-gen-openapiv2={}".format(ctx.executable._plugin.path),
            "--openapiv2_out=allow_merge=true,merge_file_name={},openapi_naming_strategy=simple,{}:{}".format(
                merge_file_name,
                go_import_path,
                ctx.bin_dir.path,
            ),
        ] + import_roots + [sources[0].path],
        inputs = proto_info.transitive_sources,
        tools = [ctx.executable._plugin],
        outputs = [document],
        mnemonic = "ProtoOpenAPI",
        use_default_shell_env = True,
    )
    return DefaultInfo(files = depset([document]))

proto_openapi = rule(
    implementation = _proto_openapi_impl,
    attrs = {
        "proto": attr.label(
            doc = "The `proto_library` whose services carry `google.api.http` rules.",
            mandatory = True,
            providers = [ProtoInfo],
        ),
        "_plugin": attr.label(
            default = Label("@com_github_grpc_ecosystem_grpc_gateway_v2//protoc-gen-openapiv2"),
            executable = True,
            cfg = "exec",
        ),
        "_protoc": attr.label(
            default = Label("//devinfra/python:protoc"),
            executable = True,
            cfg = "exec",
        ),
    },
    doc = "Writes `<name>.swagger.json`, the OpenAPI 2 document of the routes in `proto`.",
)
