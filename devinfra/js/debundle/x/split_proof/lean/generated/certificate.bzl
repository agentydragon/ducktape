"""Generate Lean certificate source with the standalone generator."""

def _lowered_certificate_impl(ctx):
    certificate = ctx.outputs.out
    generator = ctx.attr.generator[DefaultInfo].files_to_run
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
    args.add_all([manifest, certificate])
    if ctx.attr.namespace:
        args.add("--namespace", ctx.attr.namespace)
    args.add_all(ctx.files.variants, before_each = "--variant")
    args.add_all(ctx.files.controls, before_each = "--control")
    ctx.actions.run(
        inputs = [manifest, ctx.file.source, ctx.file.spec] + ctx.files.variants + ctx.files.controls,
        executable = generator,
        outputs = [certificate],
        env = {"BAZEL_BINDIR": ctx.bin_dir.path},
        arguments = [args],
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
        "generator": attr.label(cfg = "exec", executable = True, mandatory = True),
        "variants": attr.label_list(allow_files = [".json"]),
    },
)
