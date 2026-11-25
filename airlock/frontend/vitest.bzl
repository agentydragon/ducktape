"""Vitest tests for the Airlock frontend."""

load("//devinfra/js:vitest.bzl", "vitest_test_for")

airlock_vitest_test = vitest_test_for(
    config = "//airlock/frontend:vitest_config",
    tsconfig = "//airlock/frontend:tsconfig",
)
