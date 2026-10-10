"""`provider_plugin_layer`: the rules_tf provider mirror as an image layer."""

def _provider_plugin_layer_impl(ctx):
    # The mirror `tf.download(mirror = ...)` in MODULE.bazel fetched for validate and lint: the
    # same pinned providers, for the host platform the repository rule ran on.
    mirror = ctx.toolchains["@rules_tf//:tf_toolchain_type"].runtime.mirror
    layer = ctx.actions.declare_file(ctx.label.name + ".tar")
    ctx.actions.run(
        executable = ctx.executable._tool,
        arguments = ["--mirror", mirror.path, "--prefix", ctx.attr.prefix, "--output", layer.path],
        inputs = [mirror],
        outputs = [layer],
        mnemonic = "ProviderPluginLayer",
        progress_message = "Unpacking Terraform providers into %{output}",
    )
    return [DefaultInfo(files = depset([layer]))]

provider_plugin_layer = rule(
    implementation = _provider_plugin_layer_impl,
    doc = "A layer tar holding every provider of the rules_tf mirror, unpacked under `prefix`.",
    attrs = {
        "prefix": attr.string(mandatory = True, doc = "Absolute directory the providers land in."),
        "_tool": attr.label(default = Label("//cluster/tf_runner:plugin_layer_bin"), executable = True, cfg = "exec"),
    },
    toolchains = ["@rules_tf//:tf_toolchain_type"],
)
