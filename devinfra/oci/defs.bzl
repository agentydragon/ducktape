"""OCI build helpers for this repository."""

load("@aspect_bazel_lib//lib:paths.bzl", "to_rlocation_path")
load("@rules_oci//oci:defs.bzl", "oci_load")

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
    """Standalone .rloc target for images that already have oci_load elsewhere.

    Use this for images that need `bazel run :load` AND a test data dep.
    Generates :<name> (.rloc file; add to data= deps in tests).

    In tests, load via util.oci.load_oci_image with an OciImage constant:
        from third_party.containers.rlocations import MY_IMAGE
        load_oci_image(MY_IMAGE)
    """
    _oci_layout_rloc(
        name = name,
        image = image,
        visibility = visibility,
        testonly = testonly,
    )

def oci_tarball(name, image, repo_tags, visibility = None, testonly = False):
    """OCI image target for tests and `bazel run` loading.

    Generates two targets:
    - :<name>      - .rloc file; add to data= deps in tests
    - :<name>_load - oci_load target; runnable via `bazel run`

    In tests, load via util.oci.load_oci_image with an OciImage constant:
        from third_party.containers.rlocations import MY_IMAGE
        load_oci_image(MY_IMAGE)
    """
    _oci_layout_rloc(
        name = name,
        image = image,
        visibility = visibility,
        testonly = testonly,
    )
    oci_load(
        name = name + "_load",
        image = image,
        repo_tags = repo_tags,
        testonly = testonly,
    )
