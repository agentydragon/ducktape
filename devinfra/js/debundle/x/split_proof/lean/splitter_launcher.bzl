"""Launch one Lean executable with the shared Node frontend in its runfiles."""

load("@rules_shell//shell:sh_binary.bzl", "sh_binary")

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
            "{CORE}": runfile(ctx.executable.core),
            "{MODULE_IO}": runfile(ctx.executable.module_io),
        },
        is_executable = True,
    )

_splitter_launcher = rule(
    implementation = _splitter_launcher_impl,
    attrs = {
        "core": attr.label(cfg = "target", executable = True, mandatory = True),
        "module_io": attr.label(cfg = "target", executable = True, mandatory = True),
        "out": attr.output(mandatory = True),
        "template": attr.label(allow_single_file = [".sh"], mandatory = True),
    },
)

def splitter_tool(name, core, visibility, testonly = False):
    module_io = "//devinfra/js/debundle/x/split_proof/translation:module_io"
    _splitter_launcher(
        name = name + "_launcher",
        out = name + "_launcher.sh",
        core = core,
        module_io = module_io,
        template = "lowersplit.sh",
        testonly = testonly,
    )
    sh_binary(
        name = name,
        srcs = [":" + name + "_launcher"],
        data = [core, module_io],
        testonly = testonly,
        use_bash_launcher = True,
        visibility = visibility,
    )
