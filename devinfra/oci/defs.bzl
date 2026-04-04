"""OCI build helpers for this repository."""

load("@aspect_bazel_lib//lib:paths.bzl", "to_rlocation_path")

def _oci_layout_rloc_impl(ctx):
    """Write a one-line .rloc file with the OCI layout runfiles path."""
    out = ctx.actions.declare_file(ctx.label.name + ".rloc")
    ctx.actions.write(out, to_rlocation_path(ctx, ctx.file.image))

    # Merge image runfiles so the OCI layout files are accessible in tests.
    runfiles = ctx.runfiles([out]).merge(ctx.attr.image[DefaultInfo].default_runfiles)
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
