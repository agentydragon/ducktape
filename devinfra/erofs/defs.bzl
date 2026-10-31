"""Hermetic erofs image rule."""

def _erofs_image_impl(ctx):
    # A fixed UUID and timestamp, and no compression, make the image a function of the tar.
    ctx.actions.run(
        inputs = [ctx.file.tar, ctx.file._closure],
        outputs = [ctx.outputs.out],
        executable = ctx.executable._tool,
        arguments = [
            ctx.file._closure.path,
            "mkfs.erofs",
            "--tar=f",
            "--all-root",
            "--quiet",
            "-T0",
            "-U",
            "00000000-0000-0000-0000-000000000000",
            "-L",
            ctx.attr.volume_label,
            ctx.outputs.out.path,
            ctx.file.tar.path,
        ],
        mnemonic = "MkfsErofs",
        progress_message = "Building erofs image %s" % ctx.label.name,
    )
    return [DefaultInfo(files = depset([ctx.outputs.out]))]

erofs_image = rule(
    implementation = _erofs_image_impl,
    doc = "Builds an uncompressed erofs image owned by root from a tar.",
    attrs = {
        "tar": attr.label(
            allow_single_file = [".tar"],
            mandatory = True,
            doc = "The tree to hold, such as a pkg_tar. File modes and symlinks carry over.",
        ),
        "out": attr.output(mandatory = True),
        "volume_label": attr.string(mandatory = True),
        "_closure": attr.label(
            default = "@trixie_erofs_utils//:flat",
            allow_single_file = True,
        ),
        "_tool": attr.label(
            default = "//devinfra/erofs:erofs_tool_bin",
            executable = True,
            cfg = "exec",
        ),
    },
)
