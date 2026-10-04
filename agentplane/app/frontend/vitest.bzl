"""`vitest_test` for the agentplane frontend's specs, with its config and tsconfig filled in."""

load("//devinfra/js:vitest.bzl", "vitest_test")

def agentplane_vitest_test(name, srcs, **kwargs):
    """A `vitest_test` of the frontend's shared `vitest_config` and `ts_config`, in any of its packages.

    Args:
        name: The test target.
        srcs: The spec files.
        **kwargs: As for `vitest_test`, minus `config` and `tsconfig`.
    """
    vitest_test(
        name = name,
        srcs = srcs,
        config = "//agentplane/app/frontend:vitest_config",
        tsconfig = "//agentplane/app/frontend:tsconfig",
        **kwargs
    )
