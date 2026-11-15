"""Generate transient Foundation SDK JSON artifacts for recorded-data visual tests."""

load("@aspect_rules_js//js:defs.bzl", "js_run_binary")
load("//devinfra/python:defs.bzl", "py_binary")

def foundation_dashboard(name, module):
    """Build the Python module's library and emit its matching hyphenated JSON."""
    filename = module.replace("_", "-")
    py_binary(
        name = module + "_bin",
        testonly = True,
        main_module = native.package_name().replace("/", ".") + "." + module,
        deps = [":" + module],
    )
    native.genrule(
        name = module + "_raw",
        testonly = True,
        outs = [filename + ".raw.json"],
        cmd = "$(location :" + module + "_bin) > $@",
        tools = [":" + module + "_bin"],
    )
    js_run_binary(
        name = name,
        testonly = True,
        srcs = [":" + module + "_raw"],
        args = ["--parser", "json", filename + ".raw.json"],
        chdir = native.package_name(),
        stdout = filename + ".json",
        tool = "//devinfra:prettier_bin",
    )
