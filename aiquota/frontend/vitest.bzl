"""Vitest tests for the AIQuota frontend."""

load("//devinfra/js:vitest.bzl", "vitest_test_for")

aiquota_vitest_test = vitest_test_for(
    config = "//aiquota/frontend:vitest_config",
    tsconfig = "//aiquota/frontend:tsconfig",
)
