"""Module extension for container image pins.

Reads devinfra/image_pins.json and:
- Calls oci_pull for images with a "platforms" field (creates OCI repos)
- Generates @image_pins//:pins.bzl with Starlark constants for all images

Usage in MODULE.bazel:
    image_pins = use_extension("//devinfra:image_pins.bzl", "image_pins")
    image_pins.from_file(lockfile = "//devinfra:image_pins.json")
    use_repo(image_pins, "image_pins", "freecad_test", "freecad_test_linux_amd64")

In BUILD files:
    load("@image_pins//:pins.bzl", "RBE_WORKER_IMAGE", "RBE_WORKER_DIGEST")
"""

load("@rules_oci//oci:pull.bzl", "oci_pull")

_from_file = tag_class(attrs = {
    "lockfile": attr.label(mandatory = True, allow_single_file = True),
})

def _pins_repo_impl(rctx):
    """Generate pins.bzl with constants for all pinned images."""
    pins = json.decode(rctx.read(rctx.attr.lockfile))
    lines = ['"""Generated image pin constants. Do not edit -- update devinfra/image_pins.json."""', ""]
    for name, pin in sorted(pins.items()):
        upper = name.upper()
        lines.append('%s_IMAGE = "%s"' % (upper, pin["image"]))
        lines.append('%s_DIGEST = "%s"' % (upper, pin["digest"]))
    rctx.file("pins.bzl", "\n".join(lines) + "\n")
    rctx.file("BUILD.bazel", "")

_pins_repo = repository_rule(
    implementation = _pins_repo_impl,
    attrs = {"lockfile": attr.label(mandatory = True, allow_single_file = True)},
)

def _impl(module_ctx):
    direct_deps = ["image_pins"]
    for mod in module_ctx.modules:
        for cfg in mod.tags.from_file:
            _pins_repo(name = "image_pins", lockfile = cfg.lockfile)
            pins = json.decode(module_ctx.read(cfg.lockfile))
            for name, pin in pins.items():
                # Only create oci_pull repos for images with platforms
                # (i.e., images that need to be loaded as OCI tarballs in tests).
                # Images without platforms are only used as string references
                # (e.g., RBE container-image exec property).
                if "platforms" not in pin:
                    continue
                oci_pull(
                    name = name,
                    image = pin["image"],
                    digest = pin["digest"],
                    platforms = pin["platforms"],
                    is_bzlmod = True,
                )
                if mod.is_root:
                    direct_deps.append(name)
                    for platform in pin["platforms"]:
                        direct_deps.append("_".join([name] + platform.split("/")))

    return module_ctx.extension_metadata(
        root_module_direct_deps = direct_deps,
        root_module_direct_dev_deps = [],
        reproducible = True,
    )

image_pins = module_extension(
    implementation = _impl,
    tag_classes = {"from_file": _from_file},
)
