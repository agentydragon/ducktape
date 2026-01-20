"""Macro to package init scripts into tars with proper naming."""

load("@rules_pkg//pkg:tar.bzl", "pkg_tar")

def init_script_tar(name, script, visibility = ["//visibility:public"]):
    """Package an init script into /init in a tar.

    Args:
        name: Name of the pkg_tar target (e.g., "critic_agent_init_tar")
        script: Source init script file (e.g., "critic_init")
        visibility: Visibility for the tar target
    """

    # If script is already named "init", package it directly
    if script == "init":
        pkg_tar(
            name = name,
            srcs = [script],
            mode = "0755",
            package_dir = "/",
            visibility = visibility,
        )
    else:
        # Create a renamed copy for packaging
        genrule_name = name + "_gen"
        subdir = name.replace("_tar", "")

        native.genrule(
            name = genrule_name,
            srcs = [script],
            outs = [subdir + "/init"],
            cmd = "cp $< $@",
        )

        pkg_tar(
            name = name,
            srcs = [":" + genrule_name],
            mode = "0755",
            package_dir = "/",
            strip_prefix = subdir,
            visibility = visibility,
        )
