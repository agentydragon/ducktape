"""`vitest_test` for the aiquota frontend's specs, with its config and tsconfig filled in."""

load("//devinfra/js:vitest.bzl", "vitest_test")

def aiquota_vitest_test(name, srcs, **kwargs):
    """A `vitest_test` of the frontend's shared `vitest_config` and `ts_config`.

    Args:
        name: The test target.
        srcs: The spec files.
        **kwargs: As for `vitest_test`, minus `config` and `tsconfig`.
    """
    vitest_test(
        name = name,
        srcs = srcs,
        config = "//aiquota/frontend:vitest_config",
        tsconfig = "//aiquota/frontend:tsconfig",
        **kwargs
    )
