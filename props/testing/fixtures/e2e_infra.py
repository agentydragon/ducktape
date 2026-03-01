"""Testcontainers-based e2e infrastructure fixtures.

Provides test infrastructure:
- Docker registry (testcontainers registry:2, session-scoped with per-test cleanup)
- BazelImage fixtures for agent images (from Bazel oci_image layouts)

The registry container is session-scoped to avoid 28-76s startup overhead on RBE,
but manifests are deleted at the start of each test to ensure crane push always
triggers agent_definition recording via the backend proxy.

Usage in tests:
    async def test_something(e2e_registry, grader_image, e2e_stack):
        async with e2e_stack({DEFAULT_TEST_MODEL: mock}, images=[grader_image]) as stack:
            image = await stack.registry.resolve_image(AgentType.GRADER, BUILTIN_TAG)
            handle = await stack.registry.start_snapshot_grader(image=image, ...)
            await handle  # blocks until grader exits, or: await handle.kill_and_delete() to stop early
"""

from __future__ import annotations

import logging
from collections.abc import Generator

import httpx
import pytest
from opentelemetry import trace
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from props.db.models import AgentType
from third_party.containers.rlocations import REGISTRY_2_TARBALL, RYUK_TARBALL
from util.oci import BazelImage, load_image

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


# --- Infrastructure ---


@pytest.fixture(scope="session", autouse=True)
def _preload_registry_images() -> None:
    """Preload registry:2 image once per session so per-test container startup is fast."""
    load_image(RYUK_TARBALL)
    load_image(REGISTRY_2_TARBALL)


@pytest.fixture(scope="session")
def _e2e_registry_container() -> Generator[DockerContainer]:
    """Session-scoped Docker registry container (internal).

    Use e2e_registry instead, which adds per-test cleanup.
    """
    with tracer.start_as_current_span("e2e_registry startup"):
        with tracer.start_as_current_span("configure container"):
            registry = (
                DockerContainer("registry:2")
                .with_exposed_ports(5000)
                .with_env("REGISTRY_HTTP_RELATIVEURLS", "true")
                .with_env("REGISTRY_STORAGE_DELETE_ENABLED", "true")
                .waiting_for(LogMessageWaitStrategy("listening on"))
            )

        with tracer.start_as_current_span("container start + wait"):
            registry.start()

    try:
        yield registry
    finally:
        registry.stop()


@tracer.start_as_current_span("delete registry manifests")
def _delete_all_manifests(registry_url: str) -> None:
    """Delete all agent manifests from registry to ensure clean state for next test."""
    for agent_type in AgentType:
        repo = agent_type.value
        # List tags for this repo
        resp = httpx.get(f"{registry_url}/v2/{repo}/tags/list", timeout=5.0)
        if resp.status_code == 404:
            continue  # Repo doesn't exist yet
        resp.raise_for_status()
        tags = resp.json().get("tags") or []

        # Delete each tag's manifest
        for tag in tags:
            # Get manifest digest
            head_resp = httpx.head(
                f"{registry_url}/v2/{repo}/manifests/{tag}",
                headers={"Accept": "application/vnd.oci.image.manifest.v1+json"},
                timeout=5.0,
            )
            if head_resp.status_code != 200:
                continue
            digest = head_resp.headers.get("Docker-Content-Digest")
            if not digest:
                continue

            # Delete by digest
            httpx.delete(f"{registry_url}/v2/{repo}/manifests/{digest}", timeout=5.0).raise_for_status()
            logger.debug(f"Deleted {repo}:{tag} ({digest})")


@pytest.fixture
def e2e_registry(_e2e_registry_container: DockerContainer) -> DockerContainer:
    """Function-scoped registry fixture that clears manifests before each test.

    The underlying container is session-scoped (avoids 28-76s startup on RBE),
    but manifests are deleted at the start of each test so crane push always
    triggers agent_definition recording via the proxy.
    """
    registry_url = f"http://localhost:{_e2e_registry_container.get_exposed_port(5000)}"
    _delete_all_manifests(registry_url)
    return _e2e_registry_container


@pytest.fixture
def e2e_registry_url(e2e_registry: DockerContainer) -> str:
    """Raw registry URL (e.g., 'http://localhost:32769') for PROPS_REGISTRY_UPSTREAM_URL."""
    port = e2e_registry.get_exposed_port(5000)
    return f"http://localhost:{port}"


# --- Agent image fixtures (session-scoped, from Bazel oci_image layouts) ---


# Map of repo_name → OCI layout rlocation
_AGENT_IMAGES = {
    "critic": "_main/props/agents/critic/image",
    "grader": "_main/props/agents/grader/image",
    "critic_dev_optimize": "_main/props/agents/critic_dev/optimize/image",
    "critic_dev_improve": "_main/props/agents/critic_dev/improve/image",
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
critic_dev_optimize_image = _make_image_fixture("critic_dev_optimize")
critic_dev_improve_image = _make_image_fixture("critic_dev_improve")
