"""`vitest_test` for Props frontend specs, sharing its config and TypeScript settings."""

load("//devinfra/js:vitest.bzl", "vitest_test")

def props_vitest_test(name, srcs, **kwargs):
    vitest_test(
        name = name,
        srcs = srcs,
        config = "//props/frontend:vitest_config",
        tsconfig = "//props/frontend:tsconfig",
        **kwargs
    )
