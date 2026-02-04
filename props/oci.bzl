"""OCI image helpers for Bazel py_binary targets on distroless base images.

rules_python's py_binary generates a bash bootstrap script that locates the
hermetic Python interpreter in runfiles and exec's it. distroless images have
no shell, so the bash wrapper fails with "no such file or directory".

These helpers compute the CMD and env that bypass the bash wrapper by directly
invoking the hermetic Python interpreter and stage2 bootstrap from runfiles.

TODO: This is a workaround — we're hand-computing internal rules_python paths
(venv layout, stage2 bootstrap) that could break on rules_python upgrades.
Investigate whether rules_python or rules_oci have a first-class solution for
distroless py_binary containers, or whether switching to a non-distroless base
with a shell (e.g. cc-debian) would be simpler.
"""

def py_binary_distroless_cmd(binary_name, binary_package = None):
    """Compute CMD for a distroless container running a Bazel py_binary.

    Args:
        binary_name: Name of the py_binary target (e.g., "critic").
        binary_package: Bazel package of the py_binary (e.g., "props/critic").
            Defaults to the calling BUILD file's package.

    Returns:
        CMD list for oci_image.
    """
    pkg = binary_package or native.package_name()
    runfiles = "/app/{}.runfiles".format(binary_name)
    return [
        "{}/_main/{}/_{}.venv/bin/python3".format(runfiles, pkg, binary_name),
        "{}/_main/{}/_{}_stage2_bootstrap.py".format(runfiles, pkg, binary_name),
    ]

def py_binary_distroless_env(binary_name, extra_env = {}):
    """Compute env for a distroless container running a Bazel py_binary.

    Args:
        binary_name: Name of the py_binary target (e.g., "critic").
        extra_env: Additional env vars to merge.

    Returns:
        Env dict for oci_image.
    """
    env = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONSAFEPATH": "1",
        "RUNFILES_DIR": "/app/{}.runfiles".format(binary_name),
    }
    env.update(extra_env)
    return env
