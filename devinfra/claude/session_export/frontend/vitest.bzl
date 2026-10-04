"""`vitest_test` for the session export frontend's specs, with its config and tsconfig filled in."""

load("//devinfra/js:vitest.bzl", "vitest_test")

def session_export_vitest_test(name, srcs, **kwargs):
    """A `vitest_test` of the frontend's shared `vitest_config` and `ts_config`.

    Args:
        name: The test target.
        srcs: The spec files.
        **kwargs: As for `vitest_test`, minus `config` and `tsconfig`.
    """
    vitest_test(
        name = name,
        srcs = srcs,
        config = "//devinfra/claude/session_export/frontend:vitest_config",
        tsconfig = "//devinfra/claude/session_export/frontend:tsconfig",
        **kwargs
    )
