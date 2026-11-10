"""Expose external proto source closures under a first-party virtual import root."""

load("@aspect_bazel_lib//lib:copy_file.bzl", "COPY_FILE_TOOLCHAINS", "copy_file_action")
load("@protobuf//bazel:proto_library.bzl", "proto_library")
load("@protobuf//bazel/common:proto_common.bzl", "proto_common")
load("@protobuf//bazel/common:proto_info.bzl", "ProtoInfo")

def _virtual_proto_sources_impl(ctx):
    excluded = {
        proto_common.get_import_path(source): True
        for dep in ctx.attr.deps
        for source in dep[ProtoInfo].transitive_sources.to_list()
    }
    sources = {}
    for proto in ctx.attr.protos:
        for source in proto[ProtoInfo].transitive_sources.to_list():
            path = proto_common.get_import_path(source)
            if path.startswith("/") or path.startswith("../") or "/../" in path:
                fail("Invalid proto import path %s for %s" % (path, source))
            if path in excluded:
                continue
            previous = sources.get(path)
            if previous and previous != source:
                fail("Multiple proto sources have import path %s: %s and %s" % (path, previous, source))
            sources[path] = source

    outputs = []
    for path in sorted(sources):
        output = ctx.actions.declare_file(ctx.label.name + "/" + path)
        copy_file_action(ctx, sources[path], output)
        outputs.append(output)
    return [DefaultInfo(files = depset(outputs))]

_virtual_proto_sources = rule(
    implementation = _virtual_proto_sources_impl,
    attrs = {
        "deps": attr.label_list(providers = [ProtoInfo]),
        "protos": attr.label_list(providers = [ProtoInfo], mandatory = True),
    },
    toolchains = COPY_FILE_TOOLCHAINS,
)

def virtual_proto_library(name, protos, deps = [], **kwargs):
    """Place upstream proto closures in a first-party virtual import root.

    Sources whose logical names are supplied by deps remain dependencies rather than copies;
    those deps must provide compatible versions of the overlapping schemas.
    Declare a wrapper once per external graph; ordinary proto_library users depend on it.
    """
    if not protos:
        fail("%s requires at least one proto" % name)
    sources = "_" + name + "_sources"
    source_kwargs = {"testonly": kwargs["testonly"]} if "testonly" in kwargs else {}
    _virtual_proto_sources(name = sources, protos = protos, deps = deps, **source_kwargs)
    proto_library(
        name = name,
        srcs = [":" + sources],
        strip_import_prefix = "/" + (native.package_name() + "/" if native.package_name() else "") + sources,
        deps = deps,
        **kwargs
    )
