import pytest_bazel
from fastmcp.client import Client

from mcp_infra.exec.docker.server import ContainerExecServer


async def test_inproc_container_exec_exposes_container_info_resource(docker_exec_server: ContainerExecServer) -> None:
    """in-proc container exec exposes a container.info resource."""

    # Call the server directly to read the resource; no manager needed here
    async with Client(docker_exec_server) as sess:
        res = await sess.read_resource_mcp(ContainerExecServer.CONTAINER_INFO_URI)
        assert res.contents, "container.info returned no contents"


if __name__ == "__main__":
    pytest_bazel.main()
