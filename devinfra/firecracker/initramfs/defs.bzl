"""Hermetic cpio archive rule (newc format)."""

def _cpio_archive_impl(ctx):
    output = ctx.outputs.out

    inputs = []
    manifest_entries = []
    for src_target, dest_path in ctx.attr.files.items():
        src_files = src_target.files.to_list()
        if len(src_files) != 1:
            fail("Each entry in 'files' must resolve to exactly one file, got %d for %s" % (len(src_files), src_target.label))
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

_cpio_archive = rule(
    implementation = _cpio_archive_impl,
    attrs = {
        # Internal: keyed by label because Bazel has no string_keyed_label_dict.
        # Public API uses the cpio_archive macro below which inverts the dict.
        "files": attr.label_keyed_string_dict(
            allow_files = True,
        ),
        "out": attr.output(mandatory = True),
        "_tool": attr.label(
            default = "//devinfra/firecracker/initramfs:cpio_tool",
            executable = True,
            cfg = "exec",
        ),
    },
)

def cpio_archive(files = {}, **kwargs):
    """Build a hermetic newc-format cpio archive.

    Args:
        files: dict mapping archive destination path → source file label.
               Example: {"./init": "//some:binary"}
        **kwargs: forwarded to the underlying rule (name, out, visibility, …).
    """
    _cpio_archive(
        files = {label: dest for dest, label in files.items()},
        **kwargs
    )
