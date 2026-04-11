"""Hermetic cpio archive rule (newc format)."""

CpioEntryInfo = provider(
    doc = "A single file together with its destination path in a cpio archive.",
    fields = {
        "dest": "Destination path in the archive (string).",
        "src": "Source File.",
    },
)

def _cpio_entry_impl(ctx):
    return [
        CpioEntryInfo(dest = ctx.attr.dest, src = ctx.file.src),
        DefaultInfo(files = depset([ctx.file.src])),
    ]

cpio_entry = rule(
    implementation = _cpio_entry_impl,
    doc = "Declares a file together with where it should appear in a cpio archive.",
    attrs = {
        "src": attr.label(allow_single_file = True, mandatory = True),
        "dest": attr.string(mandatory = True, doc = "Destination path in the archive."),
    },
)

def _cpio_archive_impl(ctx):
    output = ctx.outputs.out

    inputs = []
    manifest_entries = []
    for src in ctx.attr.srcs:
        entry = src[CpioEntryInfo]
        manifest_entries.append([entry.dest, entry.src.path])
        inputs.append(entry.src)

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
    doc = "Builds a hermetic newc-format cpio archive from cpio_entry targets.",
    attrs = {
        "srcs": attr.label_list(
            providers = [CpioEntryInfo],
            doc = "cpio_entry targets to include in the archive.",
        ),
        "out": attr.output(mandatory = True),
        "_tool": attr.label(
            default = "//devinfra/firecracker/initramfs:cpio_tool",
            executable = True,
            cfg = "exec",
        ),
    },
)
