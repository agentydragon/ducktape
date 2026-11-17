"""Declare cdk8s output as a reusable Bazel tree artifact."""

def _synthesized_manifests_impl(ctx):
    output = ctx.actions.declare_directory(ctx.label.name)
    args = ctx.actions.args()
    args.add("--output-root", output.path)

    ctx.actions.run(
        executable = ctx.executable.generator,
        arguments = [args],
        tools = [ctx.attr.generator[DefaultInfo].files_to_run, ctx.file.node],
        outputs = [output],
        env = {"JSII_NODE": ctx.file.node.path},
        mnemonic = "SynthesizeClusterManifests",
        progress_message = "Synthesizing cluster manifests for %{label}",
    )

    return [DefaultInfo(
        files = depset([output]),
        runfiles = ctx.runfiles(files = [output]),
    )]

synthesized_manifests = rule(
    implementation = _synthesized_manifests_impl,
    attrs = {
        "generator": attr.label(
            mandatory = True,
            executable = True,
            cfg = "exec",
        ),
        "node": attr.label(
            mandatory = True,
            allow_single_file = True,
            cfg = "exec",
        ),
    },
)
