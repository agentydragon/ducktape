"""`vitest_test` for Spend UI specs, sharing its config and TypeScript settings."""

load("//devinfra/js:vitest.bzl", "vitest_test")

def spend_vitest_test(name, srcs, **kwargs):
    vitest_test(
        name = name,
        srcs = srcs,
        config = "//finance/plaid/spend/ui:vitest_config",
        tsconfig = "//finance/plaid/spend/ui:tsconfig",
        **kwargs
    )
