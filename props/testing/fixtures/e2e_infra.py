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
    async def test_something(e2e_registry, grader_image, e2e_stack):
        async with e2e_stack(mock) as stack:
            run_id = await stack.registry.run_snapshot_grader(...)
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Generator
from dataclasses import dataclass

import docker
import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from test_util.docker import load_bazel_image

logger = logging.getLogger(__name__)


@dataclass
class LoadedImage:
    """Image loaded into Docker daemon from Bazel, ready for push."""

    repo_name: str
    local_tag: str


def push_image_to_proxy(docker_client: docker.DockerClient, image: LoadedImage, proxy_url: str) -> str:
    """Push a loaded image through the backend proxy (which records agent_definitions).

    Tags the image for the proxy URL and pushes via Docker SDK. The proxy
    forwards to the raw registry and records the digest in agent_definitions.

    Returns:
        The registry tag that was pushed (e.g., "localhost:12345/grader:latest")
    """
    registry_tag = f"{proxy_url}/{image.repo_name}:latest"
    local_image = docker_client.images.get(image.local_tag)
    local_image.tag(registry_tag)

    docker_client.images.push(registry_tag)
    logger.info(f"Pushed {image.local_tag} → {registry_tag} (through proxy)")

    return registry_tag


def cleanup_registry_tags(docker_client: docker.DockerClient, tags: list[str]) -> None:
    """Remove registry tags from Docker daemon."""
    for tag in tags:
        with contextlib.suppress(docker.errors.ImageNotFound):
            docker_client.images.remove(tag, force=True)


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
        wait_for_logs(registry, "listening on")
        time.sleep(0.5)
        yield registry


@pytest.fixture(scope="session")
def e2e_registry_url(e2e_registry: DockerContainer) -> str:
    """Raw registry URL (e.g., 'http://localhost:32769') for PROPS_REGISTRY_UPSTREAM_URL."""
    port = e2e_registry.get_exposed_port(5000)
    return f"http://localhost:{port}"


# --- Agent image fixtures (session-scoped, load only) ---


def _make_image_fixture(load_script: str, repo_name: str, local_tag: str):
    """Factory for agent image fixtures that load from Bazel tar."""

    @pytest.fixture(scope="session")
    def _fixture() -> LoadedImage:
        load_bazel_image(load_script, local_tag)
        logger.info(f"Loaded image {local_tag} from {load_script}")
        return LoadedImage(repo_name=repo_name, local_tag=local_tag)

    return _fixture


critic_image = _make_image_fixture("props/critic/load.sh", "critic", "critic-agent:latest")
grader_image = _make_image_fixture("props/grader/load.sh", "grader", "grader-agent:latest")
prompt_optimizer_image = _make_image_fixture(
    "props/critic_dev/optimize/load.sh", "prompt_optimizer", "prompt-optimizer-agent:latest"
)
improvement_image = _make_image_fixture("props/critic_dev/improve/load.sh", "improvement", "improvement-agent:latest")
