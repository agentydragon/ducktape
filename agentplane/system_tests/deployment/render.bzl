"""Run a suite-owned Python chart recipe and declare its serialized output."""

def _render_fixture_impl(ctx):
    output = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.run(
        executable = ctx.attr.renderer[DefaultInfo].files_to_run,
        tools = [ctx.file._node],
        outputs = [output],
        env = {"JSII_NODE": ctx.file._node.path, "K3S_FIXTURE_OUTPUT": output.path},
        mnemonic = "RenderK3sFixture",
    )
    return [DefaultInfo(files = depset([output]))]

render_fixture = rule(
    implementation = _render_fixture_impl,
    attrs = {
        "renderer": attr.label(executable = True, cfg = "exec", mandatory = True),
        "_node": attr.label(default = "@nodejs_linux_amd64//:bin/node", allow_single_file = True, cfg = "exec"),
    },
)
