"""Generate Lean certificate source with the runfiles-aware splitter."""

def _lowered_certificate_impl(ctx):
    certificate = ctx.outputs.out
    splitter = ctx.attr.splitter[DefaultInfo].files_to_run
    manifest = ctx.actions.declare_file(ctx.label.name + ".deployment.json")
    prefix = "../" * len(manifest.dirname.split("/"))
    ctx.actions.write(manifest, json.encode({
        "entry": ctx.attr.entry,
        "chunks": [{
            "path": ctx.attr.entry,
            "source": prefix + ctx.file.source.path,
            "spec": prefix + ctx.file.spec.path,
        }],
    }))
    args = ctx.actions.args()
    args.add_all([splitter.executable, manifest, certificate])
    if ctx.attr.namespace:
        args.add("--namespace", ctx.attr.namespace)
    args.add_all(ctx.files.variants, before_each = "--variant")
    args.add_all(ctx.files.controls, before_each = "--control")
    ctx.actions.run_shell(
        inputs = [manifest, ctx.file.source, ctx.file.spec] + ctx.files.variants + ctx.files.controls,
        tools = [splitter],
        outputs = [certificate],
        env = {"BAZEL_BINDIR": ctx.bin_dir.path},
        arguments = [args],
        command = """
set -eu
work=$(mktemp -d "$(dirname "$3")/split.XXXXXX")
trap 'rm -rf "$work"' EXIT
splitter=$1 manifest=$2 instance=$3
shift 3
"$splitter" --diagnostic "$manifest" "$work" --lean "$instance" "$@"
""",
        mnemonic = "GenerateSplitCertificate",
    )

lowered_certificate = rule(
    implementation = _lowered_certificate_impl,
    attrs = {
        "entry": attr.string(mandatory = True),
        "controls": attr.label_list(allow_files = [".json"]),
        "namespace": attr.string(),
        "out": attr.output(mandatory = True),
        "source": attr.label(allow_single_file = [".json", ".mjs"], mandatory = True),
        "spec": attr.label(allow_single_file = [".json"], mandatory = True),
        "splitter": attr.label(cfg = "exec", executable = True, mandatory = True),
        "variants": attr.label_list(allow_files = [".json"]),
    },
)
