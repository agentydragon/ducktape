"""A `cc_library` and its whole dependency subtree built with `--compilation_mode=opt`.

CP-SAT built without `NDEBUG` runs extra checks on every solve and logs
`CP-SAT is running in debug mode`; it is about an order of magnitude slower. A
linked library follows the compilation mode of the target that links it, so
without this rule `bazel run`, tests and any `fastbuild` invocation would link a
debug-mode solver. The transition covers OR-Tools, protobuf and Abseil together:
`NDEBUG`-dependent inline code must not mix across the libraries of one binary.
"""

load("@rules_cc//cc/common:cc_info.bzl", "CcInfo")

def _opt_transition_impl(_settings, _attr):
    return {"//command_line_option:compilation_mode": "opt"}

_opt_transition = transition(
    implementation = _opt_transition_impl,
    inputs = [],
    outputs = ["//command_line_option:compilation_mode"],
)

def _optimized_cc_library_impl(ctx):
    # A transitioned attr always arrives as a list of targets.
    dep = ctx.attr.dep[0]
    return [dep[CcInfo], DefaultInfo(files = dep[DefaultInfo].files)]

optimized_cc_library = rule(
    implementation = _optimized_cc_library_impl,
    attrs = {
        "dep": attr.label(
            mandatory = True,
            providers = [CcInfo],
            cfg = _opt_transition,
            doc = "`cc_library` to rebuild in `opt`; link it through this target, never directly.",
        ),
    },
    doc = "Forwards `dep`'s `CcInfo`, built in `opt` whatever configuration the consumer has.",
)
