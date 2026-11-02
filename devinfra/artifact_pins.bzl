"""HTTP artifact repositories whose authoritative checksums are also available to BUILD files."""

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive", "http_file")

def _pins_repo_impl(rctx):
    lines = ['"""Generated artifact checksums. Update the module extension tags in MODULE.bazel."""', ""]
    for name, digest in sorted(rctx.attr.digests.items()):
        lines.append('%s_SHA256 = "%s"' % (name.upper(), digest))
    rctx.file("pins.bzl", "\n".join(lines) + "\n")
    rctx.file("BUILD.bazel", "")

_pins_repo = repository_rule(
    implementation = _pins_repo_impl,
    attrs = {"digests": attr.string_dict(mandatory = True)},
)

_COMMON_ATTRS = {
    "name": attr.string(mandatory = True),
    "sha256": attr.string(mandatory = True),
    "urls": attr.string_list(mandatory = True),
}

_file = tag_class(attrs = _COMMON_ATTRS | {
    "downloaded_file_path": attr.string(),
    "executable": attr.bool(default = False),
})

_archive = tag_class(attrs = _COMMON_ATTRS | {
    "build_file_content": attr.string(mandatory = True),
    "strip_prefix": attr.string(mandatory = True),
})

def _impl(module_ctx):
    digests = {}
    direct_deps = ["artifact_pins"]
    for mod in module_ctx.modules:
        for pin in mod.tags.file:
            http_file(
                name = pin.name,
                sha256 = pin.sha256,
                urls = pin.urls,
                downloaded_file_path = pin.downloaded_file_path,
                executable = pin.executable,
            )
            digests[pin.name] = pin.sha256
            if mod.is_root:
                direct_deps.append(pin.name)
        for pin in mod.tags.archive:
            http_archive(
                name = pin.name,
                sha256 = pin.sha256,
                urls = pin.urls,
                strip_prefix = pin.strip_prefix,
                build_file_content = pin.build_file_content,
            )
            digests[pin.name] = pin.sha256
            if mod.is_root:
                direct_deps.append(pin.name)
    _pins_repo(name = "artifact_pins", digests = digests)
    return module_ctx.extension_metadata(
        root_module_direct_deps = direct_deps,
        root_module_direct_dev_deps = [],
        reproducible = True,
    )

artifact_pins = module_extension(
    implementation = _impl,
    tag_classes = {"file": _file, "archive": _archive},
)
