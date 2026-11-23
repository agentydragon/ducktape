"""Vitest tests for the Session Export frontend."""

load("//devinfra/js:vitest.bzl", "vitest_test_for")

session_export_vitest_test = vitest_test_for(
    config = "//devinfra/claude/session_export/frontend:vitest_config",
    tsconfig = "//devinfra/claude/session_export/frontend:tsconfig",
)
