"""Run a Go controller test binary as PID 1 in a private PID namespace."""

import asyncio
import os

import pytest_bazel

from third_party.containers import debian_slim, ryuk
from util.bazel.runfiles import get_required_path
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer


async def test_go_suite() -> None:
    for image in (ryuk.IMAGE, debian_slim.IMAGE):
        load_oci_image(image)
    binary = get_required_path(os.environ["PID1_GO_TEST_RUNFILE"]).resolve()
    container = (
        LoggedContainer(debian_slim.IMAGE.tag, test_name="pid1-go-test")
        .with_kwargs(network_mode="host", user="0:0", pids_limit=512, mem_limit="1g")
        .with_volume_mapping(str(binary), "/go-test", mode="ro")
        .with_command("/go-test -test.v")
    )
    with container:
        result = await asyncio.to_thread(container.get_wrapped_container().wait)
        assert result["StatusCode"] == 0, "Go test failed; see pid1-go-test/container.log"


if __name__ == "__main__":
    pytest_bazel.main()
