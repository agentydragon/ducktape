"""Testcontainers-based e2e infrastructure fixtures.

Provides hermetic e2e test infrastructure using testcontainers:
- Docker registry for agent images
- Image loading from Bazel :load targets
- Network configuration for agent containers

This eliminates the need for docker-compose and CI workflow infrastructure setup.
Images are loaded from Bazel data dependencies, making tests fully hermetic.

Usage in BUILD.bazel:
    py_test(
        name = "test_e2e",
        srcs = ["test_e2e.py"],
        data = [
            "//props/critic:load",
            "//props/grader:load",
        ],
        deps = ["//props/testing/fixtures"],
    )

Usage in tests:
    @pytest.mark.requires_docker
    async def test_something(e2e_registry, critic_image, e2e_stack):
        # critic_image fixture loads and pushes the critic image
        async with e2e_stack(mock) as stack:
            run_id = await stack.registry.run_critic(...)
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass

import docker
import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from test_util.docker import load_bazel_image

logger = logging.getLogger(__name__)


@dataclass
class AgentImage:
    """Loaded and pushed agent image info."""

    repo_name: str
    local_tag: str
    registry_tag: str


@contextmanager
def load_and_push_image(
    docker_client: docker.DockerClient, registry_url: str, load_script_path: str, repo_name: str, local_tag: str
) -> Generator[AgentImage]:
    """Context manager to load an image via Bazel :load script and push to registry.

    Args:
        docker_client: Docker client
        registry_url: URL of the registry (e.g., "localhost:5000")
        load_script_path: Runfiles path to the load.sh script
        repo_name: Repository name for the image (e.g., "critic")
        local_tag: Local Docker tag after load (e.g., "critic-agent:latest")

    Yields:
        AgentImage with load details

    Cleanup:
        Removes the registry tag after the context exits.
    """
    load_bazel_image(load_script_path, local_tag)

    # Get the loaded image
    image = docker_client.images.get(local_tag)
    image_id = image.id or "unknown"
    logger.info(f"Loaded image {image_id[:19]} as {local_tag}")

    # Tag for the registry
    registry_tag = f"{registry_url}/{repo_name}:latest"
    image.tag(registry_tag)
    logger.info(f"Tagged as {registry_tag}")

    # Push to registry
    docker_client.images.push(registry_tag)
    logger.info(f"Pushed {registry_tag}")

    try:
        yield AgentImage(repo_name=repo_name, local_tag=local_tag, registry_tag=registry_tag)
    finally:
        # Cleanup: remove registry tag
        with contextlib.suppress(docker.errors.ImageNotFound):
            docker_client.images.remove(registry_tag, force=True)


# --- Registry configuration ---


@dataclass
class E2ERegistryConfig:
    """Registry configuration for e2e tests.

    Stores individual components (host, port) and builds env vars on demand.
    """

    host_host: str
    host_port: str
    container_host: str
    container_port: str

    def as_env_vars(self) -> dict[str, str]:
        """Build environment variables for oci_utils.py configuration."""
        return {
            "PROPS_REGISTRY_HOST": self.host_host,
            "PROPS_REGISTRY_PORT": self.host_port,
            "PROPS_REGISTRY_PROXY_URL": f"http://{self.host_host}:{self.host_port}",
            "PROPS_PROXY_CONTAINER_NAME": self.container_host,
            "PROPS_PROXY_CONTAINER_PORT": self.container_port,
        }


# --- Session-scoped infrastructure ---


@pytest.fixture(scope="session")
def docker_client() -> Generator[docker.DockerClient]:
    """Session-scoped Docker client."""
    client = docker.from_env()
    yield client
    client.close()


@pytest.fixture(scope="session")
def e2e_registry() -> Generator[DockerContainer]:
    """Session-scoped Docker registry for e2e tests.

    Starts a registry:2 container and waits for it to be ready.
    """
    with DockerContainer("registry:2").with_exposed_ports(5000) as registry:
        # Wait for registry to be ready
        wait_for_logs(registry, "listening on")
        time.sleep(0.5)
        yield registry


@pytest.fixture(scope="session")
def e2e_registry_config(e2e_registry: DockerContainer) -> E2ERegistryConfig:
    """Registry configuration for e2e tests."""
    port = str(e2e_registry.get_exposed_port(5000))
    container_host = os.environ.get("PROPS_E2E_HOST_HOSTNAME", "host.docker.internal")
    return E2ERegistryConfig(host_host="localhost", host_port=port, container_host=container_host, container_port=port)


# --- Agent image fixtures ---


def _make_image_fixture(load_script: str, repo_name: str, local_tag: str):
    """Factory for agent image fixtures."""

    @pytest.fixture(scope="session")
    def _fixture(docker_client: docker.DockerClient, e2e_registry_config: E2ERegistryConfig) -> Generator[AgentImage]:
        registry_url = f"{e2e_registry_config.host_host}:{e2e_registry_config.host_port}"
        with load_and_push_image(docker_client, registry_url, load_script, repo_name, local_tag) as image:
            yield image

    return _fixture


critic_image = _make_image_fixture("props/critic/load.sh", "critic", "critic-agent:latest")
grader_image = _make_image_fixture("props/grader/load.sh", "grader", "grader-agent:latest")
prompt_optimizer_image = _make_image_fixture(
    "props/critic_dev/optimize/load.sh", "prompt_optimizer", "prompt-optimizer:latest"
)
improvement_image = _make_image_fixture("props/critic_dev/improve/load.sh", "improvement", "improvement-agent:latest")


# --- Environment application ---


@pytest.fixture
def e2e_env(e2e_registry_config: E2ERegistryConfig, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Apply e2e environment variables for a test.

    Use this fixture to configure oci_utils.py to use the testcontainers registry.
    """
    env_vars = e2e_registry_config.as_env_vars()
    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)
    return env_vars
