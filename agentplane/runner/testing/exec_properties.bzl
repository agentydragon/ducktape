"""Run native runner integration tests with the deployed Pod's unprivileged identity."""

NATIVE_RUNNER_EXEC_PROPERTIES = {
    "test.dockerUser": "1000:1000",
    "test.nonroot-workspace": "true",
}

# Database-backed native tests reach their VM-local Docker daemon without a root-only socket.
NATIVE_RUNNER_DOCKER_EXEC_PROPERTIES = NATIVE_RUNNER_EXEC_PROPERTIES | {
    "test.enable-dockerd-tcp": "true",
}
