"""Bazel rules for specimen tar generation and testing."""

load("@rules_pkg//pkg:mappings.bzl", "pkg_files", "strip_prefix")
load("//tools/testing:defs.bzl", "py_test")

def _create_code_tar_impl(ctx):
    """Implementation for create_code_tar rule."""
    srcs = ctx.files.srcs
    if not srcs:
        fail("create_code_tar: srcs is empty")

    # For external repo files, auto-detect strip prefix from workspace root.
    # For local files, use the explicit strip_prefix attribute.
    first_src = srcs[0]
    ws_root = first_src.owner.workspace_root
    if ws_root:
        strip_prefix = ws_root
    elif ctx.attr.strip_prefix:
        strip_prefix = ctx.attr.strip_prefix
    else:
        fail("strip_prefix is required for local source files")

    args = ctx.actions.args()
    args.add("code-tar")
    args.add(ctx.outputs.out)
    args.add("--strip-prefix", strip_prefix)
    args.add_all(srcs)

    ctx.actions.run(
        inputs = srcs,
        outputs = [ctx.outputs.out],
        executable = ctx.executable._tool,
        arguments = [args],
        mnemonic = "CreateCodeTar",
        progress_message = "Creating code tar for %s" % ctx.label.name,
    )

    return [DefaultInfo(files = depset([ctx.outputs.out]))]

create_code_tar = rule(
    implementation = _create_code_tar_impl,
    attrs = {
        "srcs": attr.label_list(allow_files = True),
        "strip_prefix": attr.string(
            doc = "Path prefix to strip from source files. Auto-detected for external repos.",
        ),
        "out": attr.output(mandatory = True),
        "_tool": attr.label(
            default = "//props/specimens:compile",
            executable = True,
            cfg = "exec",
        ),
    },
)

def _create_data_blob_impl(ctx):
    """Implementation for create_data_blob rule."""
    args = ctx.actions.args()
    args.add("data-blob")
    args.add(ctx.outputs.out)
    args.add(ctx.attr.snapshot_slug)
    args.add(ctx.attr.split)
    args.add_all(ctx.files.issue_files)

    ctx.actions.run(
        inputs = ctx.files.issue_files,
        outputs = [ctx.outputs.out],
        executable = ctx.executable._tool,
        arguments = [args],
        mnemonic = "CreateDataBlob",
        progress_message = "Creating data blob for %s" % ctx.label.name,
    )

    return [DefaultInfo(files = depset([ctx.outputs.out]))]

create_data_blob = rule(
    implementation = _create_data_blob_impl,
    attrs = {
        "issue_files": attr.label_list(allow_files = [".yaml"]),
        "snapshot_slug": attr.string(mandatory = True),
        "split": attr.string(mandatory = True),
        "out": attr.output(mandatory = True),
        "_tool": attr.label(
            default = "//props/specimens:compile",
            executable = True,
            cfg = "exec",
        ),
    },
)

def specimen_targets(name, slug, split, code_srcs, code_strip_prefix = ""):
    """Generate bundle artifacts and test target for a specimen.

    Args:
        name: Base name for generated targets (typically "specimen")
        slug: Specimen slug in format "{repo}/{date}" (e.g., "ducktape/2026-01-17-00")
        split: Dataset split (e.g., "train", "test", "val")
        code_srcs: Label list for code files. For local specimens, pass glob(["code/**/*"]).
            For remote-VCS specimens, pass the http_archive filegroup label.
        code_strip_prefix: Override strip prefix for code tar. If empty, uses
            "{package}/code" for local or auto-detects for external repos.

    Generates:
        - {name}_code_tar: Deterministic uncompressed tar of code/ with BUILD.bazel restored
        - {name}_data_blob: YAML with {split, issues} structure
        - test_{name}: py_test validating this specimen
    """
    code_tar_target = name + "_code_tar"
    data_blob_target = name + "_data_blob"

    if not code_strip_prefix:
        pkg = native.package_name()
        code_strip_prefix = pkg + "/code"

    create_code_tar(
        name = code_tar_target,
        srcs = code_srcs,
        strip_prefix = code_strip_prefix,
        out = name + "_code.tar",
        visibility = ["//props:__subpackages__"],
    )

    create_data_blob(
        name = data_blob_target,
        issue_files = native.glob(["issues/**/*.yaml"]),
        snapshot_slug = slug,
        split = split,
        out = name + "_data.yaml",
        visibility = ["//props:__subpackages__"],
    )

    # Per-specimen test
    py_test(
        name = "test_" + name,
        srcs = ["//props/specimens:test_specimen.py"],
        data = [
            ":" + code_tar_target,
            ":" + data_blob_target,
        ],
        env = {
            "SPECIMEN_CODE_TAR": "$(location :" + code_tar_target + ")",
            "SPECIMEN_DATA_YAML": "$(location :" + data_blob_target + ")",
        },
        imports = ["../.."],
        requires_docker = True,
        tags = ["specimen"],
        deps = [
            "//util/bazel:runfiles",
            "//props:conftest",
            "//props/db:config",
            "//props/db:database",
            "//props/db:models",
            "//props/db:setup",
            "//props/db/sync",
            "//util:oci",
            "@pypi//pytest",
            "@pypi//pytest_bazel",
            "@pypi//sqlalchemy",
            "@pypi//testcontainers",
        ],
    )

def specimen_pkg_files(name, slug, specimen_package, visibility = None):
    """Create pkg_files placing a specimen's artifacts under /specimens/{slug}/."""
    pkg_files(
        name = name,
        srcs = [
            specimen_package + ":specimen_code_tar",
            specimen_package + ":specimen_data_blob",
        ],
        prefix = "/specimens/" + slug,
        strip_prefix = strip_prefix.files_only(),
        visibility = visibility,
    )
