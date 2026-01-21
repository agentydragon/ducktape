"""Rule to copy workspace status file for Python to parse at runtime."""

def _build_info_impl(ctx):
    """Copy workspace status file to a data file Python can read."""
    output = ctx.actions.declare_file("_build_status.txt")

    # Just copy the stable status file - Python will parse it
    ctx.actions.run_shell(
        outputs = [output],
        inputs = [ctx.info_file],
        command = "cp '{info_file}' '{output}'".format(
            info_file = ctx.info_file.path,
            output = output.path,
        ),
    )

    return [DefaultInfo(files = depset([output]))]

build_info = rule(
    implementation = _build_info_impl,
    attrs = {},
)
