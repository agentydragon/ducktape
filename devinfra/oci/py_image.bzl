"""OCI image helpers for py_image_layer containers.

Uses aspect_rules_py's py_image_layer for multi-layer OCI images
(interpreter, site-packages, app code) on a debian-slim base with bash.
The aspect py_binary launcher is a bash script that sets up a venv and
exec's the Python interpreter, so the base image must provide /bin/bash.
"""

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

    When binary_name is provided, sets PATH and PYTHONPATH so that
    `python3` is findable and `import props` works from bare exec calls.

    Args:
        binary_name: Name of the aspect_py_binary target. When set, adds
            the venv bin/ to PATH and runfiles _main/ to PYTHONPATH.
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
        runfiles_main = "/{}/{}.runfiles/_main".format(pkg, binary_name)
        env["PATH"] = "{}:/usr/local/bin:/usr/bin:/bin".format(venv_bin)
        env["PYTHONPATH"] = runfiles_main
    env.update(extra_env)
    return env

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
