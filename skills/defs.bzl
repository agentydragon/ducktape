load("@rules_pkg//pkg:mappings.bzl", "pkg_files", "strip_prefix")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")

def skill_package(name, srcs, visibility = None):
    """Package a skill's deployable files (excludes BUILD, evals, etc.)."""
    pkg_files(
        name = name + "_files",
        srcs = srcs,
        strip_prefix = strip_prefix.from_pkg(),
    )
    pkg_tar(
        name = name + "_tar",
        srcs = [":" + name + "_files"],
        package_dir = name,
        visibility = visibility or ["//visibility:public"],
    )
    native.filegroup(
        name = name,
        srcs = srcs,
        visibility = visibility or ["//visibility:public"],
    )
