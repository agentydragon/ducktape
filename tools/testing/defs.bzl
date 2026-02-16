"""Default test wrappers with sensible defaults for this repository."""

load("@rules_python//python:defs.bzl", _py_test = "py_test")

def py_test(name, size = "small", requires_docker = False, tags = None, **kwargs):
    """py_test wrapper with sensible defaults for this repository.

    Provides:
    - size='small' default (60s timeout) for fast unit tests
    - Optional Docker tag via requires_docker parameter

    Note: Docker exec properties (Firecracker, init-dockerd, recycle-runner)
    are configured globally in //:rbe_linux_x64 platform exec_properties.

    Args:
        name: Target name.
        size: Test size. Defaults to 'small' (60s timeout).
        requires_docker: Whether this test needs Docker. If True, adds the
            "requires_docker" tag for filtering. Defaults to False.
        tags: Additional tags. Must not include "requires_docker" (use the parameter).
        **kwargs: Passed through to py_test (exec_properties, data, env, etc).
    """
    base_tags = tags or []

    # Ensure tag is not set explicitly - use the parameter instead
    if "requires_docker" in base_tags:
        fail("Use requires_docker parameter instead of 'requires_docker' tag in {}".format(name))

    if requires_docker:
        base_tags = base_tags + ["requires_docker"]

    _py_test(
        name = name,
        size = size,
        tags = base_tags,
        **kwargs
    )
