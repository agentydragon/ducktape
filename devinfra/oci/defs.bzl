"""OCI build helpers for this repository."""

load("@aspect_bazel_lib//lib:paths.bzl", "to_rlocation_path")
load("//devinfra/python:defs.bzl", "py_library")

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

    Prefer `oci_layout_py` for Python consumers. Use this for language-neutral
    handoffs that need a file containing the layout's runfiles path.
    """
    _oci_layout_rloc(
        name = name,
        image = image,
        visibility = visibility,
        testonly = testonly,
    )

_LAYOUT_MODULE = """\"\"\"Auto-generated OCI layout runfiles path (do not edit).\"\"\"

LAYOUT_RLOCATION = {rloc}
"""

def _oci_layout_py_src_impl(ctx):
    """Write a Python module holding this image's runfiles path."""

    # Named for the module, not for this target: the file has to land at
    # <package>/<module>.py or the import path it is generated for does not exist.
    out = ctx.actions.declare_file(ctx.attr.module + ".py")
    ctx.actions.write(out, _LAYOUT_MODULE.format(
        # Computed rather than written by hand: a runfiles path starts with a repository
        # name, and this one is `_main/...` inside this repo but `ducktape+/...` from a
        # module that depends on it. A literal would be right in exactly one of the two.
        rloc = repr(to_rlocation_path(ctx, ctx.file.image)),
    ))
    return [DefaultInfo(files = depset([out]))]

_oci_layout_py_src = rule(
    implementation = _oci_layout_py_src_impl,
    attrs = {
        "module": attr.string(mandatory = True),
        "image": attr.label(mandatory = True, allow_single_file = True),
    },
)

def oci_layout_py(name, image, visibility = None, testonly = False):
    """A py_library exporting `LAYOUT_RLOCATION` and carrying `image` in its runfiles.

    One dep gives a test both the constant and the bytes it names:

        # BUILD.bazel
        deps = ["//third_party/containers:postgres_18"]

        # the test
        from third_party.containers import postgres_18
        load_oci_image(postgres_18.LAYOUT_RLOCATION)

    The library's `data` keeps the generated runfiles path paired with the layout.

    Args:
        name: target and module name, e.g. "postgres_18" for `postgres_18.LAYOUT_RLOCATION`.
        image: the OCI image layout target, e.g. "@postgres_18_linux_amd64".
        visibility: target visibility.
        testonly: mark the generated module and library testonly.
    """
    _oci_layout_py_src(
        name = name + "_src",
        module = name,
        image = image,
        testonly = testonly,
    )
    py_library(
        name = name,
        srcs = [":" + name + "_src"],
        data = [image],
        testonly = testonly,
        visibility = visibility,
    )
