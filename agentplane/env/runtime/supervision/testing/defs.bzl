"""Run Go tests that exercise Pod native supervision as PID 1."""

load("@io_bazel_rules_go//go:def.bzl", "go_test")
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
        env = {
            "DOCKER_HOST": "tcp://127.0.0.1:2375",
            "PID1_GO_TEST_IMAGE": "agentplane.env.runtime.supervision.testing.git_tools" if git else "third_party.containers.debian_slim",
            "PID1_GO_TEST_RUNFILE": "_main/{}/{}_/{}".format(native.package_name(), binary, binary),
        },
        exec_properties = {
            "test.dockerUser": "1000:1000",
            "test.enable-dockerd-tcp": "true",
            "test.nonroot-workspace": "true",
        },
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
