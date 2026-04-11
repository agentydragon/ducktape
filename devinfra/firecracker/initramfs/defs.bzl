"""Hermetic cpio archive rule (newc format)."""

def _cpio_archive_impl(ctx):
    output = ctx.outputs.out
    args = ctx.actions.args()
    args.add(output)

    inputs = []
    for src_target, dest_path in ctx.attr.files.items():
        src_files = src_target.files.to_list()
        if len(src_files) != 1:
            fail("Each entry in 'files' must resolve to exactly one file, got %d for %s" % (len(src_files), src_target.label))
        src_file = src_files[0]
        args.add(dest_path)
        args.add(src_file)
        inputs.append(src_file)

    ctx.actions.run(
        inputs = inputs,
        outputs = [output],
        executable = ctx.executable._tool,
        arguments = [args],
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
