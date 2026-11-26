"""Python integration tests that run the Environment Runtime with Docker."""

load("//agentplane/env/e2e/testing:exec_properties.bzl", "NATIVE_RUNNER_DOCKER_ENV", "NATIVE_RUNNER_DOCKER_EXEC_PROPERTIES")
load("//devinfra/python:defs.bzl", "py_test")

def runtime_integration_test(uses_pinned_cli = True, **kwargs):
    """Run with the Runtime Pod's unprivileged identity and VM-local Docker."""

    # The pinned native Codex and Claude binaries are built for Linux x86_64.
    py_test(
        env = NATIVE_RUNNER_DOCKER_ENV,
        exec_properties = NATIVE_RUNNER_DOCKER_EXEC_PROPERTIES,
        requires_docker = True,
        target_compatible_with = ["@platforms//cpu:x86_64", "@platforms//os:linux"] if uses_pinned_cli else None,
        **kwargs
    )
