from __future__ import annotations

from mcp_infra.exec.docker.server import ContainerExecServer


class RuntimeServer(ContainerExecServer):
    """Runtime server (container exec) with typed tool access.

    This is a thin wrapper around ContainerExecServer for semantic clarity.
    """
