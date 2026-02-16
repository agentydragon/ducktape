"""Bazel rules for specimen tar generation and testing."""

load("//tools/testing:defs.bzl", "py_test")

def _create_code_tar_impl(ctx):
    """Implementation for create_code_tar rule."""
    args = ctx.actions.args()
    args.add(ctx.outputs.out)
    if ctx.attr.root_marker != "code":
        args.add("--root-marker")
        args.add(ctx.attr.root_marker)
    args.add_all(ctx.files.srcs)

    ctx.actions.run(
        inputs = ctx.files.srcs,
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
        "root_marker": attr.string(default = "code"),
        "out": attr.output(mandatory = True),
        "_tool": attr.label(
            default = "//props/specimens:create_code_tar",
            executable = True,
            cfg = "exec",
        ),
    },
)

def _create_data_blob_impl(ctx):
    """Implementation for create_data_blob rule."""
    args = ctx.actions.args()
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
            default = "//props/specimens:create_data_blob",
            executable = True,
            cfg = "exec",
        ),
    },
)

def specimen_targets(name, slug, split, code_srcs, code_root_marker = "code"):
    """Generate bundle artifacts and test target for a specimen.

    Args:
        name: Base name for generated targets (typically "specimen")
        slug: Specimen slug in format "{repo}/{date}" (e.g., "ducktape/2026-01-17-00")
        split: Dataset split (e.g., "train", "test", "val")
        code_srcs: Label list for code files. For local specimens, pass glob(["code/**/*"]).
            For remote-VCS specimens, pass the http_archive filegroup label.
        code_root_marker: Path segment that marks the root of source files in code_srcs.
            The tar tool extracts relative paths after this segment. Defaults to "code".

    Generates:
        - {name}_code_tar: Deterministic uncompressed tar of code/ with BUILD.bazel restored
        - {name}_data_blob: YAML with {split, issues} structure
        - test_{name}: py_test validating this specimen
    """
    code_tar_target = name + "_code_tar"
    data_blob_target = name + "_data_blob"

    # Create code tar using custom Starlark rule (no shell!)
    create_code_tar(
        name = code_tar_target,
        srcs = code_srcs,
        root_marker = code_root_marker,
        out = name + "_code.tar",
    )

    # Create data blob using custom Starlark rule (no shell!)
    create_data_blob(
        name = data_blob_target,
        issue_files = native.glob(["issues/**/*.yaml"]),
        snapshot_slug = slug,
        split = split,
        out = name + "_data.yaml",
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
        tags = ["integration", "specimen"],
        deps = [
            "//bazel_util:runfiles",
            "//props:conftest",
            "//props/db:config",
            "//props/db:database",
            "//props/db:models",
            "//props/db:setup",
            "//props/db/sync",
            "//test_util:image_loader",
            "@pypi//pytest",
            "@pypi//pytest_bazel",
            "@pypi//sqlalchemy",
            "@pypi//testcontainers",
        ],
    )
