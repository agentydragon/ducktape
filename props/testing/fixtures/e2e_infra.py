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

import logging
import os
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import docker
import pytest
from rules_python.python.runfiles import runfiles
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

logger = logging.getLogger(__name__)


@dataclass
class AgentImage:
    """Loaded and pushed agent image info."""

    repo_name: str
    local_tag: str
    registry_tag: str


def _get_runfiles_path(relative_path: str) -> Path:
    """Get path to a file in Bazel runfiles."""
    r = runfiles.Create()
    path = r.Rlocation(f"_main/{relative_path}")
    if path:
        return Path(path)

    # Fallback: check bazel-bin for local dev
    repo_root = Path(__file__).parent.parent.parent.parent
    return repo_root / "bazel-bin" / relative_path


@contextmanager
def load_and_push_image(
    docker_client: docker.DockerClient,
    registry_url: str,
    load_script_path: str,
    repo_name: str,
    local_tag: str,
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
    script_path = _get_runfiles_path(load_script_path)

    # Run the load script to load image into Docker
    logger.info(f"Loading {repo_name} image via {script_path}")
    result = subprocess.run(
        [script_path],
        capture_output=True,
        text=True,
        env={**os.environ, "DOCKER_CLI_EXPERIMENTAL": "enabled"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to load {repo_name} image: {result.stderr}")

    # Get the loaded image
    image = docker_client.images.get(local_tag)
    logger.info(f"Loaded image {image.id[:19]} as {local_tag}")

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
        try:
            docker_client.images.remove(registry_tag, force=True)
        except docker.errors.ImageNotFound:
            pass


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
def e2e_registry_url(e2e_registry: DockerContainer) -> str:
    """URL of the e2e registry (for pushing/pulling from host)."""
    port = e2e_registry.get_exposed_port(5000)
    return f"localhost:{port}"


@pytest.fixture(scope="session")
def e2e_registry_container_url(e2e_registry: DockerContainer) -> str:
    """URL of the e2e registry (for pulling from containers).

    Uses host.docker.internal for bridge networking.
    """
    port = e2e_registry.get_exposed_port(5000)
    host = os.environ.get("PROPS_E2E_HOST_HOSTNAME", "host.docker.internal")
    return f"{host}:{port}"


# --- Agent image configurations ---

# Maps fixture name -> (load_script_path, repo_name, local_tag)
AGENT_IMAGE_CONFIGS: dict[str, tuple[str, str, str]] = {
    "critic_image": ("props/critic/load.sh", "critic", "critic-agent:latest"),
    "grader_image": ("props/grader/load.sh", "grader", "grader-agent:latest"),
    "prompt_optimizer_image": ("props/critic_dev/optimize/load.sh", "prompt_optimizer", "prompt-optimizer:latest"),
    "improvement_image": ("props/critic_dev/improve/load.sh", "improvement", "improvement-agent:latest"),
}


def _make_image_fixture(load_script: str, repo_name: str, local_tag: str):
    """Factory for agent image fixtures."""

    @pytest.fixture(scope="session")
    def _fixture(docker_client: docker.DockerClient, e2e_registry_url: str) -> Generator[AgentImage]:
        with load_and_push_image(docker_client, e2e_registry_url, load_script, repo_name, local_tag) as image:
            yield image

    return _fixture


# Generate fixtures for each agent
critic_image = _make_image_fixture(*AGENT_IMAGE_CONFIGS["critic_image"])
grader_image = _make_image_fixture(*AGENT_IMAGE_CONFIGS["grader_image"])
prompt_optimizer_image = _make_image_fixture(*AGENT_IMAGE_CONFIGS["prompt_optimizer_image"])
improvement_image = _make_image_fixture(*AGENT_IMAGE_CONFIGS["improvement_image"])


# --- Environment configuration ---


@pytest.fixture(scope="session")
def e2e_env_vars(e2e_registry_url: str, e2e_registry_container_url: str) -> dict[str, str]:
    """Environment variables for e2e tests.

    Sets up the registry URLs so oci_utils.py uses the testcontainers registry.
    """
    host, port = e2e_registry_url.split(":")
    container_host, container_port = e2e_registry_container_url.split(":")

    return {
        # For host-side operations (resolve_image_ref)
        "PROPS_REGISTRY_HOST": host,
        "PROPS_REGISTRY_PORT": port,
        "PROPS_REGISTRY_PROXY_URL": f"http://{e2e_registry_url}",
        # For container-side operations
        "PROPS_PROXY_CONTAINER_NAME": container_host,
        "PROPS_PROXY_CONTAINER_PORT": container_port,
    }


@pytest.fixture
def e2e_env(e2e_env_vars: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Apply e2e environment variables for a test.

    Use this fixture to configure oci_utils.py to use the testcontainers registry.
    """
    for key, value in e2e_env_vars.items():
        monkeypatch.setenv(key, value)
    return e2e_env_vars
