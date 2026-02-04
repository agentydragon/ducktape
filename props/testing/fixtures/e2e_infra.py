"""Testcontainers-based e2e infrastructure fixtures.

Provides session-scoped test infrastructure:
- Docker registry (testcontainers registry:2)
- BazelImage fixtures for agent images (from Bazel oci_image layout directories)

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

import runfiles
from test_util.oci import BazelImage

logger = logging.getLogger(__name__)


# --- Session-scoped infrastructure ---


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


# --- Agent image fixtures (session-scoped, from Bazel oci_image outputs) ---


def _make_image_fixture(image_runfiles_path: str, repo_name: str):
    """Factory for agent image fixtures from Bazel oci_image layout directories."""

    @pytest.fixture(scope="session")
    def _fixture() -> BazelImage:
        layout_dir = runfiles.get_required_path(f"_main/{image_runfiles_path}")
        return BazelImage(repo_name=repo_name, layout_dir=layout_dir)

    return _fixture


critic_image = _make_image_fixture("props/critic/image", "critic")
grader_image = _make_image_fixture("props/grader/image", "grader")
prompt_optimizer_image = _make_image_fixture("props/critic_dev/optimize/image", "prompt_optimizer")
improvement_image = _make_image_fixture("props/critic_dev/improve/image", "improvement")
