"""Vitest tests for the Plaid Spend frontend."""

load("//devinfra/js:vitest.bzl", "vitest_test_for")

spend_vitest_test = vitest_test_for(
    config = "//finance/plaid/spend/ui:vitest_config",
    tsconfig = "//finance/plaid/spend/ui:tsconfig",
)
