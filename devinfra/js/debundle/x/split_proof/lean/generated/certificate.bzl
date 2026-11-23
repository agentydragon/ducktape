"""Generate Lean certificate source with the runfiles-aware splitter."""

def _lowered_certificate_impl(ctx):
    certificate = ctx.outputs.out
    splitter = ctx.attr.splitter[DefaultInfo].files_to_run
    args = ctx.actions.args()
    args.add_all([splitter.executable, ctx.file.source, ctx.file.spec, certificate])
    if ctx.attr.namespace:
        args.add("--namespace", ctx.attr.namespace)
    args.add_all(ctx.files.variants, before_each = "--variant")
    args.add_all(ctx.files.controls, before_each = "--control")
    ctx.actions.run_shell(
        inputs = [ctx.file.source, ctx.file.spec] + ctx.files.variants + ctx.files.controls,
        tools = [splitter],
        outputs = [certificate],
        env = {"BAZEL_BINDIR": ctx.bin_dir.path},
        arguments = [args],
        command = """
set -eu
work=$(mktemp -d "$(dirname "$4")/split.XXXXXX")
trap 'rm -rf "$work"' EXIT
splitter=$1 source=$2 spec=$3 instance=$4
shift 4
"$splitter" --diagnostic "$source" "$spec" "$work" --lean "$instance" "$@"
""",
        mnemonic = "GenerateSplitCertificate",
    )

lowered_certificate = rule(
    implementation = _lowered_certificate_impl,
    attrs = {
        "controls": attr.label_list(allow_files = [".json"]),
        "namespace": attr.string(),
        "out": attr.output(mandatory = True),
        "source": attr.label(allow_single_file = [".json", ".mjs"], mandatory = True),
        "spec": attr.label(allow_single_file = [".json"], mandatory = True),
        "splitter": attr.label(cfg = "exec", executable = True, mandatory = True),
        "variants": attr.label_list(allow_files = [".json"]),
    },
)
