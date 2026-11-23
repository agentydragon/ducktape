"""Vitest tests for the Augur frontend."""

load("//devinfra/js:vitest.bzl", "vitest_test_for")

augur_vitest_test = vitest_test_for(
    config = "//finance/augur/frontend:vitest_config",
    tsconfig = "//finance/augur/frontend:tsconfig",
)
