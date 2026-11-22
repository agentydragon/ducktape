"""Lint the local entry points of the JavaScript targets in a package."""

load("@npm_ducktape//:eslint/package_json.bzl", eslint_bin = "bin")

def eslint_entry_points(name):
    """Lint each local JavaScript entry point once."""
    files = {}
    for target in native.existing_rules().values():
        if target["kind"] not in ("js_binary", "js_test"):
            continue
        entry = target.get("entry_point")
        if type(entry) == "string" and entry.endswith(".mjs") and not entry.startswith(("//", "@")):
            files[entry.removeprefix(":")] = True
    paths = sorted(files)
    if not paths:
        fail("No local JavaScript entry points in " + native.package_name())
    native.filegroup(name = name + "_inputs", srcs = paths, testonly = True)
    inputs = ":" + name + "_inputs"
    eslint_bin.eslint_test(
        name = name,
        args = ["--config", "$(rootpath //:eslintrc)"] + [native.package_name() + "/" + p for p in paths],
        data = ["//:eslintrc", inputs],
        no_copy_to_bin = [inputs],
    )
