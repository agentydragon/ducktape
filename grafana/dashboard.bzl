"""Foundation SDK dashboard libraries with independently cached JSON outputs."""

load("@aspect_bazel_lib//lib:expand_template.bzl", "expand_template")
load("//devinfra/python:defs.bzl", "py_binary", "py_library")

def grafana_dashboard(name, srcs, deps = [], json_name = None, visibility = None):
    """Build a dashboard library and JSON shared by synthesis and browser tests.

    The single source module exports build_dashboard(). JSON defaults to the
    library name with underscores replaced by hyphens, followed by .json.
    """
    if len(srcs) != 1 or not srcs[0].endswith(".py"):
        fail("grafana_dashboard requires one Python source module")
    py_library(
        name = name,
        srcs = srcs,
        deps = deps,
        visibility = visibility,
    )
    expand_template(
        name = name + "_export_main",
        template = "//grafana:export_dashboard.py.tpl",
        out = name + "_export.py",
        substitutions = {
            "{{MODULE}}": native.package_name().replace("/", ".") + "." + srcs[0][:-3].replace("/", "."),
        },
        visibility = ["//visibility:private"],
    )
    py_binary(
        name = name + "_export",
        srcs = [":" + name + "_export_main"],
        main = name + "_export.py",
        deps = [":" + name, "//grafana:export_dashboard"],
        visibility = ["//visibility:private"],
    )
    native.genrule(
        name = name + "_json",
        outs = [json_name or name.replace("_", "-") + ".json"],
        cmd = "$(execpath :" + name + "_export) $@",
        tools = [":" + name + "_export"],
        visibility = visibility,
    )
