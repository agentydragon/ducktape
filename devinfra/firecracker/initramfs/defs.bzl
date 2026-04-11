"""Hermetic cpio archive rule (newc format)."""

load("@rules_pkg//pkg:providers.bzl", "PackageFilesInfo")

def _cpio_archive_impl(ctx):
    output = ctx.outputs.out

    inputs = []
    manifest_entries = []
    for src in ctx.attr.srcs:
        info = src[PackageFilesInfo]
        mode = info.attributes.get("mode", "0755")
        for dest, src_file in info.dest_src_map.items():
            manifest_entries.append([dest, src_file.path, mode])
            inputs.append(src_file)

    manifest = ctx.actions.declare_file(ctx.label.name + ".manifest.json")
    ctx.actions.write(manifest, json.encode(manifest_entries))

    ctx.actions.run_shell(
        inputs = inputs + [manifest],
        outputs = [output],
        tools = [ctx.executable._tool],
        command = "{tool} {output} < {manifest}".format(
            tool = ctx.executable._tool.path,
            output = output.path,
            manifest = manifest.path,
        ),
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
            default = "//devinfra/firecracker/initramfs:cpio_tool",
            executable = True,
            cfg = "exec",
        ),
    },
)
