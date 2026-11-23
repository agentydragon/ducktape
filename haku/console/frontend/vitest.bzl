"""Vitest tests for the Haku Console frontend."""

load("//devinfra/js:vitest.bzl", "vitest_test_for")

haku_vitest_test = vitest_test_for(
    config = "//haku/console/frontend:vitest_config",
    tsconfig = "//haku/console/frontend:tsconfig",
)
