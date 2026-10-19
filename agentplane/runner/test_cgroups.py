"""Exercise the Go runner's cgroup integration against an isolated Linux kernel subtree."""

import os

import pytest_bazel

from third_party.containers import debian_slim
from util.bazel.runfiles import get_required_path
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer


def test_real_cgroup_limits_and_supervisor_fencing() -> None:
    load_oci_image(debian_slim.IMAGE)
    binary = get_required_path(os.environ["RUNNER_TEST_BINARY"])
    container = LoggedContainer(
        debian_slim.IMAGE.tag,
        test_name="runner-cgroups",
        command=["/runner-test", "-test.run=^TestCgroupKernelFixture$", "-test.v"],
    )
    container.with_kwargs(privileged=True, cgroupns="private")
    container.with_volume_mapping(str(binary), "/runner-test", mode="ro")
    container.with_env("AGENTPLANE_CGROUP_KERNEL_FIXTURE", "1")
    with container:
        result = container.get_wrapped_container().wait(timeout=30)
        assert result["StatusCode"] == 0, "Go cgroup integration failed; see runner-cgroups/container.log"


if __name__ == "__main__":
    pytest_bazel.main()
