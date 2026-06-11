from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp_infra.mounted import Mounted

import aiodocker

from mcp_infra.compositor.server import Compositor
from mcp_infra.constants import WORKING_DIR
from mcp_infra.exec.container_session import BindMount, ContainerOptions
from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.prefix import MCPMountPrefix
from props.core.db.config import DbConnectionConfig

logger = logging.getLogger(__name__)

# Mount prefix for properties Docker exec server
DOCKER_MOUNT_PREFIX = MCPMountPrefix("docker")


# Docker network name for properties containers
# Agent containers connect to this network to access:
# - props-postgres (for RLS-controlled database queries)
# - props-registry-proxy (for OCI image operations with ACL enforcement)
# This network is non-internal to allow container→host communication for MCP HTTP mode
PROPS_NETWORK_NAME = "props-agents"


class PropertiesDockerCompositor(Compositor):
    """Base compositor for properties tasks - handles Docker runtime mounting.

    This intermediate class sits between Compositor and task-specific compositors (Critic, Grader, Lint).
    It centralizes Docker container setup and mounting logic that all properties tasks share.

    Hierarchy:
        Compositor (base) → mounts resources, compositor_meta
        PropertiesDockerCompositor (this class) → mounts runtime (Docker exec server)
        Task compositors (Critic/Grader/Lint) → mount task-specific servers

    Snapshots are NOT pre-mounted. Agents fetch and extract their own snapshots at init time
    via fetch_snapshot() from props.core.agent_helpers. This eliminates external dependencies at runtime.

    Attributes:
        runtime: Mounted Docker exec server (populated in __aenter__)
    """

    runtime: Mounted[ContainerExecServer]

    def __init__(
        self,
        workspace_root: Path,
        docker_client: aiodocker.Docker,
        *,
        image_id: str,
        db_conn: DbConnectionConfig | None = None,
        extra_binds: Sequence[BindMount] = (),
        workspace_mode: str = "ro",
        network_mode: str = "none",
        extra_env: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
        auto_remove: bool = False,
        container_name: str | None = None,
    ):
        super().__init__()
        self._workspace_root = workspace_root
        self._docker_client = docker_client
        self._image_id = image_id
        self._db_conn = db_conn
        self._extra_binds = extra_binds
        self._workspace_mode = workspace_mode
        self._network_mode = network_mode
        self._extra_env = extra_env
        self._labels = labels or {"adgn.project": "props", "adgn.role": "properties-runtime"}
        self._container_name = container_name
        self._auto_remove = auto_remove

    async def __aenter__(self):
        """Start compositor and mount Docker runtime server."""
        await super().__aenter__()  # Mounts resources, compositor_meta

        # Mount Docker runtime (shared by all properties compositors)
        docker_server = self._create_docker_server(self._image_id)
        self.runtime = await self.mount_inproc(DOCKER_MOUNT_PREFIX, docker_server, pinned=True)

        return self

    def _create_docker_server(self, image_id: str) -> ContainerExecServer:
        # Build Docker volume binds
        binds: list[BindMount] = [
            BindMount(host_path=self._workspace_root.resolve(), container_path=WORKING_DIR, mode=self._workspace_mode)
        ]
        if self._extra_binds:
            binds.extend(self._extra_binds)

        # Build container environment variables
        env = {
            "XDG_CACHE_HOME": "/tmp",
            "RUFF_CACHE_DIR": "/tmp/.ruff_cache",
            "MYPY_CACHE_DIR": "/tmp/.mypy_cache",
            "TMPDIR": "/tmp",
            "TMP": "/tmp",
            "TEMP": "/tmp",
            "PYTHONPYCACHEPREFIX": "/tmp/__pycache__",
        }
        if self._db_conn:
            env.update(self._db_conn.to_env_dict())
            logger.info(
                f"Set database env vars: PGHOST={self._db_conn.host}, "
                f"PGPORT={self._db_conn.port}, PGDATABASE={self._db_conn.database}, PGUSER={self._db_conn.user}"
            )
        else:
            logger.info("No db_conn provided - container will not have database access")
        if self._extra_env:
            env.update(self._extra_env)
            logger.info(f"Injecting extra environment variables: {list(self._extra_env.keys())}")

        # TODO: if we ever need fully stateless containers (new container per call),
        # add an explicit strategy switch instead of reintroducing a boolean.
        return ContainerExecServer(
            self._docker_client,
            ContainerOptions(
                image=image_id,
                working_dir=WORKING_DIR,
                binds=binds,
                environment=env,
                network_mode=self._network_mode,
                labels=self._labels,
                name=self._container_name,
                auto_remove=self._auto_remove,
            ),
        )

    @property
    def container_working_dir(self) -> Path:
        return WORKING_DIR


def build_critic_binds(
    workspace_root: Path, *, workspace_mode: str = "ro", extra_binds: dict[str, dict[str, str]] | None = None
) -> dict[str, dict[str, str]]:
    """Build standard bind mounts map for properties critic containers.

    - Mounts workspace_root at /workspace with the provided workspace_mode ("ro" or "rw")
    - Allows extra bind mounts to be merged in
    """
    binds: dict[str, dict[str, str]] = {
        str(workspace_root.resolve()): {"bind": str(WORKING_DIR), "mode": str(workspace_mode)}
    }
    if extra_binds:
        binds.update(extra_binds)
    return binds
