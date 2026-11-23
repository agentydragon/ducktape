"""Register the Lean release named by the experiment's lean-toolchain file."""

load("@rules_lean//lean:lake.bzl", "lean_dist")

def _lean_impl(ctx):
    for mod in ctx.modules:
        for tag in mod.tags.toolchain:
            spec = ctx.read(tag.version_file).strip()
            prefix = "leanprover/lean4:"
            if not spec.startswith(prefix):
                fail("Unsupported Lean toolchain: %s" % spec)
            lean_dist(name = "lean_dist", version = spec[len(prefix):])

lean = module_extension(
    implementation = _lean_impl,
    tag_classes = {
        "toolchain": tag_class(attrs = {"version_file": attr.label(mandatory = True)}),
    },
)
