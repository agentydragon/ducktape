"""Run Go tests that exercise Pod native supervision as PID 1."""

load("@io_bazel_rules_go//go:def.bzl", "go_test")
load("//agentplane/env/e2e/testing:exec_properties.bzl", "NATIVE_RUNNER_DOCKER_ENV", "NATIVE_RUNNER_DOCKER_EXEC_PROPERTIES")
load("//devinfra/python:defs.bzl", "py_test")

def pid1_go_test(name, srcs, embed, deps, git = False, size = "small", **kwargs):
    binary = name + "_binary"
    go_test(
        name = binary,
        size = size,
        srcs = srcs,
        embed = embed,
        deps = deps,
        tags = ["manual"],
        **kwargs
    )
    py_test(
        name = name,
        size = size,
        srcs = ["//agentplane/env/runtime/supervision/testing:pid1_go_test.py"],
        data = [":" + binary],
        env = NATIVE_RUNNER_DOCKER_ENV | {
            "PID1_GO_TEST_IMAGE": "agentplane.env.runtime.supervision.testing.git_tools" if git else "third_party.containers.debian_slim",
            "PID1_GO_TEST_RUNFILE": "$(rlocationpath :{})".format(binary),
        },
        exec_properties = NATIVE_RUNNER_DOCKER_EXEC_PROPERTIES,
        main = "//agentplane/env/runtime/supervision/testing:pid1_go_test.py",
        requires_docker = True,
        deps = [
            "//:conftest",
            "//third_party/containers:ryuk",
            "//devinfra/oci:oci",
            "@rules_python//python/runfiles",
            "//util/testing:container_logs",
            "@pypi//pytest_bazel",
            "@pypi//testcontainers",
        ] + (["//agentplane/env/runtime/supervision/testing:git_tools"] if git else ["//third_party/containers:debian_slim"]),
    )
