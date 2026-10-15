"""OCI image helpers for py_image_layer containers.

Uses aspect_rules_py's py_image_layer for multi-layer OCI images
(interpreter, site-packages, app code) on a debian-slim base with bash.
The aspect py_binary launcher is a bash script that sets up a venv and
exec's the Python interpreter, so the base image must provide /bin/bash.
"""

load("@aspect_rules_py//py:defs.bzl", "py_image_layer")
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

# Keep this in sync with MODULE.bazel's default Python 3.14 toolchain. Bazel's runfiles
# repository name is also embedded in aspect_rules_py launchers, so the alias below must
# preserve that exact component for rlocation().
_PYTHON_RUNTIME_REPO = "rules_python++python+python_3_14_x86_64-unknown-linux-gnu"
_PYTHON_RUNTIME_ROOT = "devinfra/oci/python_runtime/runtime_image_bin.runfiles"
_PYTHON_INTERPRETER_GROUP = "\\\\.runfiles/[^/]*python_?[0-9]+_[0-9]+(_[0-9]+)?_[a-z0-9_]+[_-](unknown|apple|pc)[_-][^/]*/"

def shared_py_image_layers(name, binary, owner = "1000", group = "1000"):
    """Return OCI layers with the binary's interpreter resolved from the shared runtime tree.

    aspect_rules_py embeds a binary-specific runfiles path in its launcher and creates its
    venv alongside that launcher. Keep the per-binary runfiles tree and add a symlink at the
    original interpreter-repository path; ship the Python toolchain files once from the
    canonical runtime binary.

    Args:
        name: Name passed to py_image_layer, usually `<binary>_layers`.
        binary: Local label of the aspect py_binary.
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
        layer_groups = {"binary_interpreter": _PYTHON_INTERPRETER_GROUP},
    )

    pkg = native.package_name()
    binary_name = binary.split(":")[-1]
    alias_name = name + "_runtime_alias"
    pkg_tar(
        name = alias_name,
        create_parents = False,
        owner = owner + "." + group,
        package_dir = "/",
        symlinks = {
            "{}/{}.runfiles/{}".format(pkg, binary_name, _PYTHON_RUNTIME_REPO): "/{}/{}".format(
                _PYTHON_RUNTIME_ROOT,
                _PYTHON_RUNTIME_REPO,
            ),
        },
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

def py_image_bytecode_layer(name, layers, owner = "1000", group = "1000"):
    """A layer of precompiled `.pyc` files for the `py_image_layer` tars in `layers`.

    The images set `PYTHONDONTWRITEBYTECODE` and run as a user that cannot write the layers, so
    without this every start compiles every module. The files are compiled by the interpreter
    the layers ship, with `unchecked-hash` invalidation: the layers are immutable, so Python need
    not stat or hash the source. `co_filename` is rewritten to the in-image path.

    Args:
        name: Target name; the output is `<name>.tar.gz`.
        layers: The `py_image_layer` tar targets, interpreter layer included.
        owner: uid owning the files.
        group: gid owning the files.
    """
    native.genrule(
        name = name,
        srcs = layers,
        outs = [name + ".tar.gz"],
        cmd = """
set -eu
root=$$(mktemp -d)
for layer in $(SRCS); do tar -xzf $$layer -C $$root; done
python=$$(find $$root -path '*/bin/python3.[0-9]*' -type f | head -n1)
$$python -m compileall -q -j0 --invalidation-mode unchecked-hash -s $$root -p / $$root
(cd $$root && find . -path '*/__pycache__/*.pyc' -print0 | tar --null -T - --owner={owner} --group={group} -czf - ) > $@
""".format(owner = owner, group = group),
    )
