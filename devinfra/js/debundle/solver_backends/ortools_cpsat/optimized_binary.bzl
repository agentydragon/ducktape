"""Re-exports an executable built with `--compilation_mode=opt`.

The wrapped binary and its whole dependency subtree are configured in `opt`
however the invoking command is configured, so consumers get the optimized build
from plain `bazel build`, `bazel test`, exec-config tool attrs and `data` deps
alike.
"""

def _opt_transition_impl(_settings, _attr):
    return {"//command_line_option:compilation_mode": "opt"}

_opt_transition = transition(
    implementation = _opt_transition_impl,
    inputs = [],
    outputs = ["//command_line_option:compilation_mode"],
)

def _optimized_binary_impl(ctx):
    # A transitioned attr always arrives as a list of targets.
    binary = ctx.attr.binary[0]

    # Runfiles are keyed by the output's own name, so naming it after this
    # target keeps `<package>/<name>` the runfiles path of the re-export.
    executable = ctx.actions.declare_file(ctx.label.name)
    ctx.actions.symlink(
        output = executable,
        target_file = ctx.executable.binary,
        is_executable = True,
    )
    return [DefaultInfo(
        executable = executable,
        runfiles = ctx.runfiles(files = [executable]).merge(binary[DefaultInfo].default_runfiles),
    )]

optimized_binary = rule(
    implementation = _optimized_binary_impl,
    attrs = {
        "binary": attr.label(
            mandatory = True,
            executable = True,
            cfg = _opt_transition,
            doc = "Executable to rebuild in `opt`.",
        ),
    },
    executable = True,
    doc = "Re-exports `binary` built in `opt`, under this target's own name.",
)
