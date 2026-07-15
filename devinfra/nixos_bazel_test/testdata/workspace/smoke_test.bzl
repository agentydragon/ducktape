def _smoke_test_impl(ctx):
    executable = ctx.actions.declare_file(ctx.label.name + ".sh")
    ctx.actions.run_shell(
        arguments = [ctx.file.src.path, executable.path],
        command = 'cp "$1" "$2" && chmod +x "$2"',
        inputs = [ctx.file.src],
        mnemonic = "SmokeTestExecutable",
        outputs = [executable],
    )
    return [DefaultInfo(executable = executable)]

smoke_test = rule(
    implementation = _smoke_test_impl,
    attrs = {
        "src": attr.label(allow_single_file = True, mandatory = True),
    },
    test = True,
)
