"""Testcontainers-based e2e infrastructure fixtures.

Provides test infrastructure:
- Docker registry (testcontainers registry:2, function-scoped to match DB)
- BazelImage fixtures for agent images (from Bazel oci_image layouts)

Usage in tests:
    @pytest.mark.requires_docker
    async def test_something(e2e_registry, grader_image, e2e_stack):
        async with e2e_stack(mock, images=[grader_image]) as stack:
            run_id = await stack.registry.run_snapshot_grader(...)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator

import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from test_util.image_loader import load_image
from test_util.oci import BazelImage
from third_party.containers.rlocations import REGISTRY_2_TARBALL, RYUK_TARBALL

logger = logging.getLogger(__name__)


# --- Infrastructure ---


@pytest.fixture(scope="session", autouse=True)
def _preload_registry_images() -> None:
    """Preload registry:2 image once per session so per-test container startup is fast."""
    load_image(RYUK_TARBALL)
    load_image(REGISTRY_2_TARBALL)


@pytest.fixture
def e2e_registry() -> Generator[DockerContainer]:
    """Function-scoped Docker registry for e2e tests.

    Function-scoped to match DB scope — each test gets a fresh registry so that
    crane push always records agent_definitions in the current test's DB.
    """
    with DockerContainer("registry:2").with_exposed_ports(5000) as registry:
        wait_for_logs(registry, "listening on")
        time.sleep(0.5)
        yield registry


@pytest.fixture
def e2e_registry_url(e2e_registry: DockerContainer) -> str:
    """Raw registry URL (e.g., 'http://localhost:32769') for PROPS_REGISTRY_UPSTREAM_URL."""
    port = e2e_registry.get_exposed_port(5000)
    return f"http://localhost:{port}"


# --- Agent image fixtures (session-scoped, from Bazel oci_image layouts) ---


# Map of repo_name → OCI layout rlocation
_AGENT_IMAGES = {
    "critic": "_main/props/critic/image",
    "grader": "_main/props/grader/image",
    "prompt_optimizer": "_main/props/critic_dev/optimize/image",
    "improvement": "_main/props/critic_dev/improve/image",
}


def _make_image_fixture(repo_name: str):
    """Factory for agent image fixtures from Bazel oci_image layouts."""
    image_rlocation = _AGENT_IMAGES[repo_name]

    @pytest.fixture(scope="session")
    def _fixture() -> BazelImage:
        return BazelImage(repo_name=repo_name, image_rlocation=image_rlocation)

    return _fixture


critic_image = _make_image_fixture("critic")
grader_image = _make_image_fixture("grader")
prompt_optimizer_image = _make_image_fixture("prompt_optimizer")
improvement_image = _make_image_fixture("improvement")
