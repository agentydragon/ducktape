"""Build a launcher with fixed runfile keys for the Lean and Node executables."""

def _splitter_launcher_impl(ctx):
    def runfile(executable):
        path = executable.short_path
        if path.startswith("../"):
            return path.removeprefix("../")
        return ctx.workspace_name + "/" + path

    ctx.actions.expand_template(
        template = ctx.file.template,
        output = ctx.outputs.out,
        substitutions = {
            "{DIAGNOSTIC}": runfile(ctx.executable.diagnostic),
            "{CORE}": runfile(ctx.executable.core),
            "{STRATEGY_CENSUS}": runfile(ctx.executable.strategy_census),
            "{MODULE_IO}": runfile(ctx.executable.module_io),
        },
        is_executable = True,
    )

splitter_launcher = rule(
    implementation = _splitter_launcher_impl,
    attrs = {
        "diagnostic": attr.label(cfg = "target", executable = True, mandatory = True),
        "core": attr.label(cfg = "target", executable = True, mandatory = True),
        "module_io": attr.label(cfg = "target", executable = True, mandatory = True),
        "strategy_census": attr.label(cfg = "target", executable = True, mandatory = True),
        "out": attr.output(mandatory = True),
        "template": attr.label(allow_single_file = [".sh"], mandatory = True),
    },
)
