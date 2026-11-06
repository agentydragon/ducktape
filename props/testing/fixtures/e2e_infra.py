"""Testcontainers-based e2e infrastructure fixtures.

Provides test infrastructure:
- Docker registry (testcontainers registry:3, session-scoped with per-test cleanup)
- BazelImage fixtures for agent images (from Bazel oci_image layouts)

Manifests are deleted at the start of each test so crane push triggers
agent_definition recording through the backend proxy. An explicit opt-in retains
only registry blobs and repository layer links across pytest processes.

Usage in tests:
    async def test_something(e2e_registry, grader_image, e2e_stack):
        async with e2e_stack({DEFAULT_TEST_MODEL: mock}, images=[grader_image]) as stack:
            image = await stack.registry.resolve_image(AgentType.GRADER, BUILTIN_TAG)
            handle = await stack.registry.start_snapshot_grader(image=image, ...)
            await handle  # blocks until grader exits, or: await handle.kill_and_delete() to stop early
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Generator
from contextlib import ExitStack

import httpx
import pytest
from opentelemetry import trace
from testcontainers.core import testcontainers_config
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from devinfra.oci.oci import BazelImage, load_oci_image
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from props.db.models import AgentType
from props.testing.fixtures.registry_blob_cache import (
    RegistryBlobCacheReceipt,
    cached_registry_container,
    registry_blob_cache_usage_bytes,
)
from third_party.containers import registry_3, ryuk

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_REGISTRY_BLOB_CACHE_ENV = "PROPS_E2E_REGISTRY_BLOB_CACHE"


def _registry_blob_cache_enabled() -> bool:
    setting = os.environ.get(_REGISTRY_BLOB_CACHE_ENV, "0")
    if setting not in {"0", "1"}:
        raise ValueError(f"{_REGISTRY_BLOB_CACHE_ENV} must be unset, '0', or '1'")
    return setting == "1"


# --- Infrastructure ---


@pytest.fixture(scope="session")
def _e2e_registry_container() -> Generator[DockerContainer]:
    """Session-scoped Docker registry container (internal).

    Use e2e_registry instead, which adds per-test cleanup.
    """
    cache_enabled = _registry_blob_cache_enabled()
    load_oci_image(ryuk.LAYOUT_RLOCATION, image_ref=testcontainers_config.ryuk_image)
    registry_image = load_oci_image(registry_3.LAYOUT_RLOCATION)
    receipt: RegistryBlobCacheReceipt | None = None
    with ExitStack() as stack:
        with tracer.start_as_current_span("e2e_registry startup"):
            with tracer.start_as_current_span("configure container"):
                registry = (
                    DockerContainer(registry_image)
                    .with_exposed_ports(5000)
                    .with_env("REGISTRY_HTTP_RELATIVEURLS", "true")
                    .with_env("REGISTRY_STORAGE_DELETE_ENABLED", "true")
                    .waiting_for(LogMessageWaitStrategy("listening on"))
                )

            with tracer.start_as_current_span("container start + wait"):
                if cache_enabled:
                    receipt = stack.enter_context(
                        cached_registry_container(
                            registry,
                            image_ref=registry_image,
                            repositories=tuple(
                                agent_type.value for agent_type in AgentType if agent_type is not AgentType.FREEFORM
                            ),
                            namespace="props-e2e",
                        )
                    )
                else:
                    stack.callback(registry.stop)
                    registry.start()
        yield registry
        if receipt is not None:
            (undeclared_outputs_dir() / "registry_blob_cache.json").write_text(
                json.dumps(
                    {
                        "namespace": receipt.namespace,
                        "recipe": receipt.recipe,
                        "volume_names": receipt.volume_names,
                        "container_id": receipt.container_id,
                        "volume_usage_bytes_before_release": registry_blob_cache_usage_bytes(registry, receipt),
                    },
                    sort_keys=True,
                )
            )


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

    The registry starts once per pytest process. Its optional named-volume cache
    retains only blobs and repository layer links across processes; manifests are
    deleted at the start of each test so crane push records definitions via proxy.
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
    "critic": "ducktape/props/agents/critic/image",
    "grader": "ducktape/props/agents/grader/image",
    "critic_dev_optimize": "ducktape/props/agents/critic_dev/optimize/image",
    "critic_dev_improve": "ducktape/props/agents/critic_dev/improve/image",
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
