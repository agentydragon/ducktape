"""Docker container executor — runs agent containers via the Docker daemon.

Implements ContainerExecutor using aiodocker. Extracted from AgentRegistry
to allow plugging in alternative runtimes (e.g., Kubernetes).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import aiodocker

from props.orchestration.executor import ContainerResult, Exited, PodInfo, PodPhase, TimedOut

logger = logging.getLogger(__name__)

# Docker container state -> runtime-agnostic PodPhase. Docker's list summary
# carries a coarse state string (no exit code), so "exited" maps to "failed";
# terminal pods are finalized by the supervisor either way.
_DOCKER_STATE_MAP: dict[str, PodPhase] = {
    "created": "pending",
    "restarting": "pending",
    "running": "running",
    "paused": "running",
    "exited": "failed",
    "dead": "failed",
}


@dataclass
class DockerContainerHandle:
    """Handle to a running Docker container."""

    container: aiodocker.containers.DockerContainer
    name: str

    async def wait(self, *, timeout_seconds: int | None) -> ContainerResult:
        """Wait for container to exit, capturing stdout/stderr."""
        container = self.container
        try:
            if timeout_seconds is not None:
                exit_info = await asyncio.wait_for(container.wait(), timeout=timeout_seconds)
            else:
                exit_info = await container.wait()

            exit_code = exit_info.get("StatusCode", 1)
            logger.info("Container %s exited with code %d", self.name, exit_code)
            exit_status: Exited | TimedOut = Exited(exit_code=exit_code)
        except TimeoutError:
            logger.exception("Container %s timed out after %d seconds", self.name, timeout_seconds)
            try:
                await container.kill()
            except aiodocker.DockerError as e:
                logger.warning("Failed to kill timed-out container: %s", e)
            exit_status = TimedOut()

        stdout = "".join(await container.log(stdout=True, stderr=False))
        stderr = "".join(await container.log(stdout=False, stderr=True))
        return ContainerResult(stdout=stdout, stderr=stderr, exit=exit_status)

    async def kill_and_delete(self) -> None:
        """Kill and remove a Docker container. Best-effort."""
        try:
            await self.container.kill()
            logger.info("Killed container %s", self.name)
        except aiodocker.DockerError as e:
            logger.warning("Failed to kill container %s: %s", self.name, e)
        try:
            await self.container.delete(force=True)
            logger.info("Deleted container %s", self.name)
        except aiodocker.DockerError as e:
            logger.warning("Failed to delete container %s: %s", self.name, e)


class DockerExecutor:
    """Runs agent containers via the local Docker daemon.

    Manages image pulling and container creation.
    """

    def __init__(
        self,
        docker_client: aiodocker.Docker,
        *,
        network_name: str,
        extra_hosts: dict[str, str] | None = None,
        pull_auth: dict[str, str] | None = None,
    ) -> None:
        self._docker_client = docker_client
        self._network_name = network_name
        self._extra_hosts = extra_hosts
        self._pull_auth = pull_auth

    async def ensure_image(self, image_ref: str) -> str:
        """Pull an OCI image to the local Docker daemon, returning its image ID."""
        try:
            info = await self._docker_client.images.inspect(image_ref)
            image_id: str = info["Id"]
            logger.info("Using cached image %s for %s", image_id[:19], image_ref)
            return image_id
        except aiodocker.DockerError as e:
            if e.status != 404:
                raise
            logger.debug("Image %s not found locally, pulling", image_ref)
        logger.info("Pulling image %s", image_ref)
        await self._docker_client.pull(image_ref, auth=self._pull_auth)
        info = await self._docker_client.images.inspect(image_ref)
        image_id = info["Id"]
        logger.info("Pulled image %s for %s", image_id[:19], image_ref)
        return image_id

    async def run_container(
        self,
        *,
        name: str,
        image_id: str,
        env: dict[str, str],
        labels: dict[str, str],
        annotations: dict[str, str] | None = None,
    ) -> DockerContainerHandle:
        """Create and start a Docker container.

        Docker has no annotations, so annotations are folded into labels (slug
        values are valid Docker label values, unlike k8s label values).
        """
        host_config: dict[str, object] = {"NetworkMode": self._network_name, "AutoRemove": False}
        if self._extra_hosts:
            host_config["ExtraHosts"] = [f"{host}:{ip}" for host, ip in self._extra_hosts.items()]

        container_config = {
            "Image": image_id,
            "Env": [f"{k}={v}" for k, v in env.items()],
            "HostConfig": host_config,
            "Labels": {**labels, **(annotations or {})},
        }

        container = await self._docker_client.containers.create(
            container_config,  # type: ignore[arg-type]  # aiodocker JSONObject
            name=name,
        )
        logger.info("Created container %s", name)

        await container.start()
        logger.info("Started container %s", name)
        return DockerContainerHandle(container=container, name=name)

    async def list_pods(self, label_selector: dict[str, str]) -> list[PodInfo]:
        filters = {"label": [f"{k}={v}" for k, v in label_selector.items()]}
        containers = await self._docker_client.containers.list(all=True, filters=filters)
        pods: list[PodInfo] = []
        for c in containers:
            summary = c._container  # list() summary dict
            names = summary.get("Names") or [summary.get("Id", "")]
            labels = summary.get("Labels") or {}
            phase: PodPhase = _DOCKER_STATE_MAP.get(summary.get("State", ""), "unknown")
            pods.append(
                PodInfo(
                    name=names[0].lstrip("/"),
                    image=summary.get("Image", ""),
                    phase=phase,
                    labels=labels,
                    annotations={},  # folded into labels on create
                )
            )
        return pods

    def handle_for(self, name: str) -> DockerContainerHandle:
        return DockerContainerHandle(container=self._docker_client.containers.container(name), name=name)

    async def close(self) -> None:
        """Close the Docker client."""
        await self._docker_client.close()
