"""OCI build helpers for this repository."""

load("@aspect_bazel_lib//lib:paths.bzl", "to_rlocation_path")

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
            default = "//devinfra/oci:extract_image_subdir",
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
    """OCI image .rloc target for use as a test data dep.

    Generates :<name> — a single-line .rloc file pointing to the OCI layout
    directory. Add to data= in tests; load at runtime via load_oci_image():
        from third_party.containers.rlocations import MY_IMAGE
        load_oci_image(MY_IMAGE)
    """
    _oci_layout_rloc(
        name = name,
        image = image,
        visibility = visibility,
        testonly = testonly,
    )
