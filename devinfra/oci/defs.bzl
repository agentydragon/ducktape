"""OCI build helpers for this repository."""

load("@aspect_bazel_lib//lib:paths.bzl", "to_rlocation_path")
load("//devinfra/python:defs.bzl", "py_library")

def _extract_image_subdir_impl(ctx):
    out_dir = ctx.actions.declare_directory(ctx.label.name)
    layout_files = ctx.attr.image[DefaultInfo].files
    args = ctx.actions.args()
    args.add("--out", out_dir.path)
    args.add("--platform", ctx.attr.platform)
    args.add("--subdir", ctx.attr.subdir)
    args.add_all(layout_files)
    ctx.actions.run(
        executable = ctx.executable._tool,
        arguments = [args],
        inputs = layout_files,
        outputs = [out_dir],
        mnemonic = "ExtractImageSubdir",
        progress_message = "Extracting %s from %s" % (ctx.attr.subdir, ctx.attr.image.label),
    )
    return [DefaultInfo(files = depset([out_dir]))]

extract_image_subdir = rule(
    implementation = _extract_image_subdir_impl,
    doc = """Extract one subdirectory from a pulled OCI image's flattened rootfs.

Useful for sourcing prebuilt assets (compiled JS, static webapps, sample data)
from a published, digest-pinned image without committing the artifacts.""",
    attrs = {
        "image": attr.label(
            mandatory = True,
            doc = "OCI image layout target (e.g. @excalidraw_linux_amd64).",
        ),
        "platform": attr.string(
            default = "linux/amd64",
            doc = "Platform to select when the image is a multi-arch index.",
        ),
        "subdir": attr.string(
            mandatory = True,
            doc = "Path inside the rootfs to extract, e.g. usr/share/nginx/html/assets.",
        ),
        "_tool": attr.label(
            default = "//devinfra/oci:extract_image_subdir_bin",
            executable = True,
            cfg = "exec",
        ),
    },
)

def _oci_layout_rloc_impl(ctx):
    """Write a one-line .rloc file with the OCI layout runfiles path."""
    out = ctx.actions.declare_file(ctx.label.name + ".rloc")
    ctx.actions.write(out, to_rlocation_path(ctx, ctx.file.image))

    # Include the image tree artifact and its runfiles so the OCI layout is
    # accessible in tests. Locally-built oci_image targets place the image
    # directory only in DefaultInfo.files, not default_runfiles, so we must
    # add it via transitive_files.
    runfiles = ctx.runfiles(
        [out],
        transitive_files = ctx.attr.image[DefaultInfo].files,
    ).merge(ctx.attr.image[DefaultInfo].default_runfiles)
    return [DefaultInfo(files = depset([out]), runfiles = runfiles)]

_oci_layout_rloc = rule(
    implementation = _oci_layout_rloc_impl,
    attrs = {
        "image": attr.label(mandatory = True, allow_single_file = True),
    },
)

def oci_layout_rloc(name, image, visibility = None, testonly = False):
    """OCI image .rloc target: a single-line file naming the OCI layout directory.

    Prefer `oci_image_py`, which wraps this in a library carrying both the Python constant
    and the image itself. Reach for this directly only where the .rloc is consumed by
    something other than `load_oci_image`.
    """
    _oci_layout_rloc(
        name = name,
        image = image,
        visibility = visibility,
        testonly = testonly,
    )

_IMAGE_MODULE = """\"\"\"Auto-generated OciImage for a pre-built container image (do not edit).\"\"\"

from util.oci import OciImage

IMAGE = OciImage("{rloc}", "{tag}")
"""

def _oci_image_py_src_impl(ctx):
    """Write a Python module holding this image's OciImage, runfiles path and all."""

    # Named for the module, not for this target: the file has to land at
    # <package>/<module>.py or the import path it is generated for does not exist.
    out = ctx.actions.declare_file(ctx.attr.module + ".py")
    ctx.actions.write(out, _IMAGE_MODULE.format(
        tag = ctx.attr.tag,
        # Computed rather than written by hand: a runfiles path starts with a repository
        # name, and this one is `_main/...` inside this repo but `ducktape+/...` from a
        # module that depends on it. A literal would be right in exactly one of the two.
        rloc = to_rlocation_path(ctx, ctx.file.rloc),
    ))
    return [DefaultInfo(files = depset([out]))]

_oci_image_py_src = rule(
    implementation = _oci_image_py_src_impl,
    attrs = {
        "module": attr.string(mandatory = True),
        "rloc": attr.label(mandatory = True, allow_single_file = True),
        "tag": attr.string(mandatory = True),
    },
)

def oci_image_py(name, image, tag, visibility = None, testonly = False):
    """A py_library exporting `IMAGE` for `image`, carrying the image in its own runfiles.

    One dep gives a test both the constant and the bytes it names:

        # BUILD.bazel
        deps = ["//third_party/containers:postgres_18"]

        # the test
        from third_party.containers import postgres_18
        load_oci_image(postgres_18.IMAGE)

    The hand-written alternative -- one module of constants, plus a separate `data` entry per
    image at every call site -- lets the two drift: import a constant, forget its data entry,
    and the test fails at run time on a missing runfile, in a package that never names it.

    Args:
        name: target and module name, e.g. "postgres_18" for `postgres_18.IMAGE`.
        image: the OCI image layout target, e.g. "@postgres_18_linux_amd64".
        tag: what the image is loaded into the Docker daemon as, e.g. "postgres:18".
        visibility: target visibility.
        testonly: mark the image and the library testonly.
    """
    oci_layout_rloc(
        name = name + "_rloc",
        image = image,
        testonly = testonly,
    )
    _oci_image_py_src(
        name = name + "_src",
        module = name,
        rloc = ":" + name + "_rloc",
        tag = tag,
        testonly = testonly,
    )
    py_library(
        name = name,
        srcs = [":" + name + "_src"],
        data = [":" + name + "_rloc"],
        testonly = testonly,
        visibility = visibility,
        deps = ["//util:oci"],
    )
