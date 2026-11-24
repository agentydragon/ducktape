"""Preserve Lean import names when a source directory is its own Bazel package."""

def _lean_module_sources_impl(ctx):
    # rules_lean derives a module's name from its path relative to the file's
    # owning package. Stage package-local sources under their Lean namespace.
    outputs = []
    for src in ctx.files.srcs:
        out = ctx.actions.declare_file(ctx.attr.module_prefix + "/" + src.basename)
        ctx.actions.symlink(output = out, target_file = src)
        outputs.append(out)
    return [DefaultInfo(files = depset(outputs))]

lean_module_sources = rule(
    implementation = _lean_module_sources_impl,
    attrs = {
        "module_prefix": attr.string(mandatory = True, doc = "Lean namespace as a slash-separated path."),
        "srcs": attr.label_list(allow_files = [".lean"], mandatory = True, doc = "Lean files beside this BUILD."),
    },
)
