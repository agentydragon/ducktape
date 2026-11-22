"""Generate Lean certificate source with the runfiles-aware splitter."""

def _lowered_certificate_impl(ctx):
    certificate = ctx.outputs.out
    splitter = ctx.attr.splitter[DefaultInfo].files_to_run
    ctx.actions.run_shell(
        inputs = [ctx.file.ast, ctx.file.spec],
        tools = [splitter],
        outputs = [certificate],
        env = {"BAZEL_BINDIR": ctx.bin_dir.path},
        arguments = [
            splitter.executable.path,
            ctx.file.ast.path,
            ctx.file.spec.path,
            certificate.path,
        ],
        command = """
set -eu
work=$(mktemp -d "$(dirname "$4")/split.XXXXXX")
trap 'rm -rf "$work"' EXIT
"$1" --diagnostic "$2" "$3" "$work" --lean "$4"
""",
        mnemonic = "GenerateSplitCertificate",
    )

lowered_certificate = rule(
    implementation = _lowered_certificate_impl,
    attrs = {
        "ast": attr.label(allow_single_file = [".json"], mandatory = True),
        "out": attr.output(mandatory = True),
        "spec": attr.label(allow_single_file = [".json"], mandatory = True),
        "splitter": attr.label(cfg = "exec", executable = True, mandatory = True),
    },
)
