"""Macros for packaging skills for deployment."""

load("@bazel_skylib//lib:paths.bzl", "paths")
load("@bazel_skylib//rules:write_file.bzl", "write_file")
load("@rules_pkg//pkg:mappings.bzl", "pkg_files", "strip_prefix")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")
load("//devinfra/python:defs.bzl", "py_library", "py_test")

_FRONTMATTER_TEST_LIB = "//skills:skill_frontmatter_test_lib"
_FRONTMATTER_VALIDATION_BIN = "//skills:frontmatter_validation_bin"
_SKILL_SPEC_LIB = "//skills:skill_spec"

def _as_label(src):
    if src.startswith("//") or src.startswith(":"):
        return src
    return ":" + src

def _is_skill_md(src):
    return paths.basename(src.split(":")[-1]) == "SKILL.md"

def _validated_skill_md(name, entry_idx, src_idx, src):
    target = "{}_validated_skill_md_{}_{}".format(name, entry_idx, src_idx)
    out = "{}_validated_skill_md_{}_{}.md".format(name, entry_idx, src_idx)
    src_label = _as_label(src)
    native.genrule(
        name = target,
        srcs = [src_label],
        outs = [out],
        cmd = "$(location {}) $(location {}) $@".format(_FRONTMATTER_VALIDATION_BIN, src_label),
        tools = [_FRONTMATTER_VALIDATION_BIN],
    )
    return ":" + target

def skill_spec_library(name, tar_basename, package_name, visibility = None):
    """Generate a py_library exporting `SPEC = SkillSpec(...)` for a skill tar.

    Eval rollouts import `<pkg>.<name>.SPEC` and pass it to `stage_skill(...)`
    to mount the skill into a sandbox container. The tar at
    `:{tar_basename}` is added as a runtime data dep.

    Args:
        name: py_library + module name (e.g. "info_gathering_skill_spec").
        tar_basename: pkg_tar target name in the same package, without ":".
        package_name: directory the tar's contents are prefixed with (the
            `package_dir` passed to pkg_tar / skill_package's `name`).
        visibility: visibility override (default //visibility:public).
    """
    spec_src = name + ".py"
    write_file(
        name = name + "_src",
        out = spec_src,
        content = [
            '"""Auto-generated SkillSpec for {} (do not edit)."""'.format(package_name),
            "",
            "from skills.skill_spec import SkillSpec",
            "",
            "SPEC = SkillSpec(",
            '    tar_rlocation="_main/{}/{}.tar",'.format(native.package_name(), tar_basename),
            '    package_name="{}",'.format(package_name),
            ")",
            "",
        ],
    )
    py_library(
        name = name,
        srcs = [spec_src],
        data = [":" + tar_basename],
        visibility = visibility or ["//visibility:public"],
        deps = [_SKILL_SPEC_LIB],
    )

def skill_mapping(srcs, prefix = "", preserve_paths = False):
    return struct(
        srcs = srcs,
        prefix = prefix,
        preserve_paths = preserve_paths,
    )

def skill_package(name, srcs = None, contents = None, visibility = None):
    """Package a skill's deployable files (excludes BUILD, evals, etc.).

    Args:
        name: Skill name / package_dir.
        srcs: Simple same-package file list. Preserves relative paths within the package.
        contents: Optional list of skill_mapping(...) entries for prefixed or cross-package files.
        visibility: Visibility override.
    """
    if srcs == None and contents == None:
        fail("skill_package requires either srcs or contents in {}".format(name))
    if srcs != None and contents != None:
        fail("skill_package accepts either srcs or contents, not both, in {}".format(name))

    entries = contents if contents != None else [skill_mapping(srcs = srcs, preserve_paths = True)]
    packaged_targets = []
    public_srcs = []
    seen_destinations = {}

    for entry_idx, entry in enumerate(entries):
        packaged_srcs = []
        renames = {}
        for src in entry.srcs:
            public_srcs.append(src)
            if entry.preserve_paths and (src.startswith("//") or ":" in src):
                fail("skill_package preserve_paths=True only supports same-package files: {}".format(src))

            if entry.preserve_paths:
                destination_without_prefix = src
            else:
                basename = paths.basename(src.split(":")[-1])
                destination_without_prefix = basename
            destination = "{}/{}".format(
                entry.prefix,
                destination_without_prefix,
            ) if entry.prefix else destination_without_prefix

            if destination in seen_destinations:
                fail("skill_package would package duplicate destination '{}' from '{}' and '{}'".format(
                    destination,
                    seen_destinations[destination],
                    src,
                ))
            seen_destinations[destination] = src
            if _is_skill_md(src):
                packaged_src = _validated_skill_md(name, entry_idx, len(packaged_srcs), src)
                renames[packaged_src] = destination_without_prefix
            else:
                packaged_src = src
            packaged_srcs.append(packaged_src)

        pkg_name = "{}_pkg_{}".format(name, entry_idx)
        pkg_files(
            name = pkg_name,
            srcs = packaged_srcs,
            prefix = entry.prefix if entry.prefix else None,
            renames = renames,
            strip_prefix = strip_prefix.from_pkg() if entry.preserve_paths else strip_prefix.files_only(),
        )
        packaged_targets.append(":" + pkg_name)

    pkg_tar(
        name = name + "_tar",
        srcs = packaged_targets,
        package_dir = name,
        visibility = visibility or ["//visibility:public"],
    )
    py_test(
        name = name + "_frontmatter_test",
        main_module = "skills.skill_frontmatter_test",
        data = [":" + name + "_tar"],
        deps = [_FRONTMATTER_TEST_LIB],
        env = {
            "SKILL_TAR": "$(location :{})".format(name + "_tar"),
        },
        visibility = visibility or ["//visibility:public"],
    )
    native.filegroup(
        name = name,
        srcs = public_srcs,
        visibility = visibility or ["//visibility:public"],
    )

    skill_spec_library(
        name = name + "_skill_spec",
        tar_basename = name + "_tar",
        package_name = name,
        visibility = visibility,
    )
