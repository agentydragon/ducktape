"""Bazel macros for tests that require Docker/container support.

On BuildBuddy RBE, these tests run inside Firecracker microVMs with a Docker
daemon started automatically via the init-dockerd exec property.

Deprecated: Use py_test(requires_docker=True) from //tools/testing:defs instead.
"""

load("//tools/testing:defs.bzl", "py_test")

def docker_py_test(name, requires_docker = True, **kwargs):
    """py_test wrapper that adds Firecracker Docker exec properties.

    Deprecated: Use py_test(requires_docker=True) from //tools/testing:defs instead.

    Args:
        name: Target name.
        requires_docker: Whether this test needs Docker. Defaults to True.
        **kwargs: Passed through to py_test.
    """
    py_test(
        name = name,
        requires_docker = requires_docker,
        **kwargs
    )
