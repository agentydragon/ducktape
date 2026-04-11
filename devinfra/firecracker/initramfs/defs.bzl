"""Hermetic cpio archive rule (newc format)."""

def _cpio_archive_impl(ctx):
    output = ctx.outputs.out

    srcs = ctx.attr.srcs
    dest_paths = ctx.attr.dest_paths
    if len(srcs) != len(dest_paths):
        fail("srcs and dest_paths must have the same length")

    inputs = []
    manifest_entries = []
    for src_target, dest_path in zip(srcs, dest_paths):
        src_files = src_target.files.to_list()
        if len(src_files) != 1:
            fail("Each entry in srcs must resolve to exactly one file, got %d for %s" % (len(src_files), src_target.label))
        src_file = src_files[0]
        manifest_entries.append([dest_path, src_file.path])
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
    attrs = {
        "srcs": attr.label_list(
            allow_files = True,
            doc = "Source file labels, parallel to dest_paths.",
        ),
        "dest_paths": attr.string_list(
            doc = "Archive destination paths, parallel to srcs.",
        ),
        "out": attr.output(mandatory = True),
        "_tool": attr.label(
            default = "//devinfra/firecracker/initramfs:cpio_tool",
            executable = True,
            cfg = "exec",
        ),
    },
)
