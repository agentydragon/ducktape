"""OCI image helpers for py_image_layer containers.

Uses aspect_rules_py's py_image_layer for multi-layer OCI images
(interpreter, site-packages, app code) on a debian-slim base with bash.
The aspect py_binary launcher is a bash script that sets up a venv and
exec's the Python interpreter, so the base image must provide /bin/bash.
"""

load("@aspect_rules_py//py:defs.bzl", "py_image_layer")
load("@rules_pkg//pkg:providers.bzl", "PackageSymlinkInfo")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")

_PY_IMAGE_ENV = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
}

def py_image_entrypoint(binary_name, binary_package = None):
    """Compute entrypoint for an OCI image running an aspect py_binary.

    Args:
        binary_name: Name of the aspect_py_binary target (e.g., "critic_bin").
        binary_package: Bazel package path. Defaults to calling BUILD's package.

    Returns:
        Entrypoint list for oci_image.
    """
    pkg = binary_package or native.package_name()
    return ["/{}/{}".format(pkg, binary_name)]

def py_image_env(binary_name = None, binary_package = None, extra_env = {}):
    """Standard env dict for py_image_layer containers.

    When binary_name is provided, sets PATH so that `python3` resolves to the
    binary's initialized virtual environment for bare exec calls. The aspect
    py_binary launcher adds first-party imports to that venv via its .pth file.

    Args:
        binary_name: Name of the aspect_py_binary target. When set, adds
            the venv bin/ to PATH.
        binary_package: Bazel package path. Defaults to calling BUILD's package.
        extra_env: Additional env vars to merge.

    Returns:
        Env dict for oci_image.
    """
    env = dict(_PY_IMAGE_ENV)
    if binary_name:
        pkg = binary_package or native.package_name()

        # The aspect py_binary launcher creates the venv at runtime inside the
        # runfiles directory as ".{binary_name}.venv".  Use that path so that
        # subprocess exec calls inherit the correct python3 after the entrypoint
        # has initialised the venv.
        venv_bin = "/{}/{}.runfiles/.{}.venv/bin".format(pkg, binary_name, binary_name)
        env["PATH"] = "{}:/usr/local/bin:/usr/bin:/bin".format(venv_bin)
    env.update(extra_env)
    return env

_PY_TOOLCHAIN_TYPE = "@bazel_tools//tools/python:toolchain_type"

PyImageRuntimeInfo = provider(fields = ["interpreter_repo_name"])

def _py_image_runtime_aspect_impl(_target, ctx):
    runtime = ctx.toolchains[_PY_TOOLCHAIN_TYPE].py3_runtime
    if runtime == None or runtime.interpreter == None:
        fail("py_image requires a Python toolchain with a hermetic py3 interpreter")
    return [PyImageRuntimeInfo(interpreter_repo_name = runtime.interpreter.owner.repo_name)]

_py_image_runtime_aspect = aspect(
    implementation = _py_image_runtime_aspect_impl,
    toolchains = [_PY_TOOLCHAIN_TYPE],
)

def _runtime_alias_impl(ctx):
    binary = ctx.attr.binary.label
    runtime_repo = ctx.attr.binary[PyImageRuntimeInfo].interpreter_repo_name
    runtime_binary = ctx.attr.runtime_binary.label
    shared_runtime_repo = ctx.attr.runtime_binary[PyImageRuntimeInfo].interpreter_repo_name
    if runtime_repo != shared_runtime_repo:
        fail("shared Python image layer uses interpreter repo {!r}, but binary {} uses {!r}".format(
            shared_runtime_repo,
            binary,
            runtime_repo,
        ))
    return [PackageSymlinkInfo(
        attributes = {},
        destination = "{}/{}.runfiles/{}".format(binary.package, binary.name, runtime_repo),
        target = "/{}/{}.runfiles/{}".format(runtime_binary.package, runtime_binary.name, runtime_repo),
    )]

_runtime_alias = rule(
    implementation = _runtime_alias_impl,
    attrs = {
        "binary": attr.label(mandatory = True, aspects = [_py_image_runtime_aspect]),
        "runtime_binary": attr.label(
            default = Label("//devinfra/oci/python_runtime:runtime_image_bin"),
            aspects = [_py_image_runtime_aspect],
        ),
    },
)

def shared_py_image_layers(name, binary, owner = "1000", group = "1000"):
    """Return OCI layers with the binary's interpreter resolved from the shared runtime tree.

    aspect_rules_py embeds a binary-specific runfiles path in its launcher and creates its
    venv alongside that launcher. Keep the per-binary runfiles tree and add a symlink at the
    selected interpreter repository path; ship the Python toolchain files once from the
    shared runtime binary.

    Args:
        name: Name passed to py_image_layer, usually `<binary>_layers`.
        binary: Label of the aspect py_binary.
        owner: UID for image files.
        group: GID for image files.

    Returns:
        Ordered labels for shared interpreter, binary default/packages, and the runfiles alias.
    """
    py_image_layer(
        name = name,
        binary = binary,
        group = group,
        owner = owner,
    )

    alias_name = name + "_runtime_alias"
    _runtime_alias(
        name = alias_name + "_symlink",
        binary = binary,
        visibility = ["//visibility:private"],
    )
    pkg_tar(
        name = alias_name,
        create_parents = False,
        owner = owner + "." + group,
        package_dir = "/",
        srcs = [":" + alias_name + "_symlink"],
    )

    return [
        "//devinfra/oci/python_runtime:runtime_interpreter_layer",
        ":" + name + "_default",
        ":" + name + "_packages",
        ":" + alias_name,
    ]

def py_python3_symlink(name, binary_name, binary_package = None):
    """Create a pkg_tar that symlinks /usr/local/bin/python3 to the venv python.

    This makes `exec(["python3", ...])` work in agent containers without
    knowing the container-specific venv path.

    The aspect py_binary launcher creates the venv lazily at runtime inside the
    runfiles directory as ".{binary_name}.venv".  The symlink therefore becomes
    valid after the container entrypoint has run for the first time.

    Args:
        name: Target name for the pkg_tar.
        binary_name: Name of the aspect_py_binary target.
        binary_package: Bazel package path. Defaults to calling BUILD's package.
    """
    pkg = binary_package or native.package_name()
    venv_python = "/{}/{}.runfiles/.{}.venv/bin/python3".format(pkg, binary_name, binary_name)
    pkg_tar(
        name = name,
        symlinks = {"/usr/local/bin/python3": venv_python},
    )
