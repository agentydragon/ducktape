"""Declare the corpus once; the manifest contains only Bazel-declared files."""

def _short_path(file):
    return file.short_path

def _corpus_files_impl(ctx):
    prefix = ctx.label.package + "/cases/"
    manifest = ctx.actions.declare_file(ctx.label.name + ".json")
    rows = []
    for file in sorted(ctx.files.srcs, key = _short_path):
        if not file.short_path.startswith(prefix):
            fail("corpus file must be below " + prefix)
        rows.append({
            "path": file.short_path.removeprefix(prefix),
            "runfile": ctx.workspace_name + "/" + file.short_path,
        })
    ctx.actions.write(manifest, json.encode(rows))
    return [DefaultInfo(
        files = depset([manifest]),
        runfiles = ctx.runfiles(files = ctx.files.srcs),
    )]

corpus_files = rule(
    implementation = _corpus_files_impl,
    attrs = {"srcs": attr.label_list(allow_files = True)},
)
