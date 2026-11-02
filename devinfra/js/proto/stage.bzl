"""Place protobuf-es outputs from an external proto in a first-party package.

External aspect outputs live outside the first-party npm dependency tree. Real copies let module
resolvers retain the consumer's package location when resolving the protobuf runtime. Callers
wrap these files in js_library and set their local generator's rewrite_imports to this output
package prefix. Only generated JS and declarations are staged; proto sources remain upstream.
"""

load("@aspect_bazel_lib//lib:copy_file.bzl", "COPY_FILE_TOOLCHAINS", "copy_file_action")
load("@aspect_rules_js//js:providers.bzl", "JsInfo")
load("@aspect_rules_js//js/private:proto.bzl", "js_proto_aspect")
load("@protobuf//bazel/common:proto_info.bzl", "ProtoInfo")

def _stage_protobuf_es_impl(ctx):
    proto = ctx.attr.proto
    info = proto[ProtoInfo]
    source_root = info.proto_source_root.removesuffix("/") + "/"
    outputs = []
    for source in info.direct_sources:
        path = source.path.removeprefix(source_root).removesuffix(".proto")
        for generated, suffix in [(proto[JsInfo].sources, "_pb.js"), (proto[JsInfo].types, "_pb.d.ts")]:
            expected = source.short_path.removesuffix(".proto") + suffix
            matches = [file for file in generated.to_list() if file.short_path == expected]
            if len(matches) != 1:
                fail("Expected one generated binding for %s, got %s" % (source.path, matches))
            file = matches[0]
            output = ctx.actions.declare_file(path + suffix)
            copy_file_action(ctx, file, output)
            outputs.append(output)
    return [DefaultInfo(files = depset(outputs))]

stage_protobuf_es = rule(
    implementation = _stage_protobuf_es_impl,
    attrs = {"proto": attr.label(aspects = [js_proto_aspect], providers = [ProtoInfo], mandatory = True)},
    toolchains = COPY_FILE_TOOLCHAINS,
)
