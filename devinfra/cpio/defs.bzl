"""Hermetic cpio archive rule (newc format)."""

load("@rules_pkg//pkg:providers.bzl", "PackageFilesInfo")

def _cpio_archive_impl(ctx):
    output = ctx.outputs.out

    # TODO: if rules_pkg ever exposes create_mapping_context_from_ctx +
    # add_label_list + write_manifest from @rules_pkg//pkg/private:pkg_files.bzl
    # as a public API, replace the manual dict construction and json.encode below
    # with those helpers (they are currently restricted to //pkg/private internals).
    inputs = []
    manifest_entries = []
    for src in ctx.attr.srcs:
        info = src[PackageFilesInfo]
        attrs = info.attributes
        entry_base = {
            "type": "file",
            "mode": attrs.get("mode"),
            "user": attrs.get("user"),
            "group": attrs.get("group"),
            "uid": attrs.get("uid"),
            "gid": attrs.get("gid"),
        }
        for dest, src_file in info.dest_src_map.items():
            manifest_entries.append(dict(entry_base, dest = dest, src = src_file.path))
            inputs.append(src_file)

    manifest = ctx.actions.declare_file(ctx.label.name + ".manifest.json")
    ctx.actions.write(manifest, json.encode(manifest_entries))

    ctx.actions.run(
        inputs = inputs + [manifest],
        outputs = [output],
        executable = ctx.executable._tool,
        arguments = [output.path, manifest.path],
        mnemonic = "CpioArchive",
        progress_message = "Building cpio archive %s" % ctx.label.name,
    )

    return [DefaultInfo(files = depset([output]))]

cpio_archive = rule(
    implementation = _cpio_archive_impl,
    doc = "Builds a hermetic newc-format cpio archive from pkg_files targets.",
    attrs = {
        "srcs": attr.label_list(
            providers = [PackageFilesInfo],
            doc = "pkg_files targets to include in the archive.",
        ),
        "out": attr.output(mandatory = True),
        "_tool": attr.label(
            default = "//devinfra/cpio:cpio_tool",
            executable = True,
            cfg = "exec",
        ),
    },
)
