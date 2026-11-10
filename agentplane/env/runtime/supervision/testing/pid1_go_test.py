"""Run a Go controller test binary as PID 1 in a private PID namespace."""

import asyncio
import importlib
import os

import pytest_bazel
from python.runfiles import Runfiles
from testcontainers.core import testcontainers_config

from devinfra.oci.oci import load_oci_image
from third_party.containers import ryuk
from util.testing.container_logs import LoggedContainer


async def test_go_suite() -> None:
    test_image = importlib.import_module(os.environ["PID1_GO_TEST_IMAGE"]).LAYOUT_RLOCATION
    load_oci_image(ryuk.LAYOUT_RLOCATION, image_ref=testcontainers_config.ryuk_image)
    image_ref = load_oci_image(test_image)
    binary = (Runfiles.CreateOrRaise().root() / os.environ["PID1_GO_TEST_RUNFILE"]).resolve()
    container = (
        LoggedContainer(image_ref, test_name="pid1-go-test")
        .with_kwargs(network_mode="host", user="0:0", pids_limit=512, mem_limit="1g")
        .with_volume_mapping(str(binary), "/go-test", mode="ro")
        .with_command("/go-test -test.v")
    )
    with container:
        result = await asyncio.to_thread(container.get_wrapped_container().wait)
        assert result["StatusCode"] == 0, "Go test failed; see pid1-go-test/container.log"


if __name__ == "__main__":
    pytest_bazel.main()
