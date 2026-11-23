"""Vitest tests for the Agentplane frontend."""

load("//devinfra/js:vitest.bzl", "vitest_test_for")

agentplane_vitest_test = vitest_test_for(
    config = "//agentplane/app/frontend:vitest_config",
    tsconfig = "//agentplane/app/frontend:tsconfig",
)
