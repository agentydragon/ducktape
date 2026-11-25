"""Vitest tests for the Props frontend."""

load("//devinfra/js:vitest.bzl", "vitest_test_for")

props_vitest_test = vitest_test_for(
    config = "//props/frontend:vitest_config",
    tsconfig = "//props/frontend:tsconfig",
)
