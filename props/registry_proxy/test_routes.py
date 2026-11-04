"""Tests for registry proxy header forwarding.

Verifies that multi-valued headers (especially Accept) are preserved
when proxying requests to the upstream registry. Docker sends multiple
Accept header lines for manifest requests, and all values must be
forwarded to the upstream registry.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib

import httpx
import pytest
import pytest_bazel
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.responses import Response

from props.core.agent_types import AgentType, CriticDevOptimizeTypeConfig, FreeformTypeConfig, TargetMetric
from props.core.oci_utils import BUILTIN_TAG
from props.db.async_database import AsyncDatabase
from props.db.database import Database
from props.db.models import AgentDefinition
from props.registry_proxy import routes
from props.registry_proxy.app import create_app
from props.registry_proxy.routes import _is_grader_builtin_push, _proxy_to_upstream
from props.testing.fixtures.credentials import make_agent_credentials
from props.testing.fixtures.runs import FAKE_CRITIC_DEV_OPTIMIZE_DIGEST, FAKE_CRITIC_DIGEST
from util.testing.asgi import serve_app_in_loop

# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep //props/testing/fixtures:db
pytest_plugins = ("util.testing.postgres_fixtures", "props.testing.fixtures.db")

DOCKER_ACCEPT_TYPES = [
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
]


async def test_proxy_preserves_multi_valued_accept_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that multiple Accept header values are all forwarded to upstream.

    Docker 28 sends separate Accept header lines for each manifest media type.
    The proxy must forward all of them, not just the last one (which is what
    dict(request.headers) does — it deduplicates keys, keeping last-value-wins).

    This test:
    1. Starts a fake upstream registry server that captures received headers
    2. Starts a proxy server that forwards via _proxy_to_upstream
    3. Sends a request through the proxy with multiple Accept headers
    4. Asserts the upstream received all Accept values
    """
    captured: list[list[tuple[str, str]]] = []

    upstream_app = FastAPI()

    @upstream_app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH"])
    async def catch_all(request: Request) -> Response:
        captured.append(list(request.headers.items()))
        return Response(content=b'{"schemaVersion": 2}', status_code=200)

    proxy_app = FastAPI()

    @proxy_app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH"])
    async def proxy(request: Request) -> Response:
        return await _proxy_to_upstream(request)

    async with serve_app_in_loop(upstream_app) as upstream_url:
        monkeypatch.setenv("PROPS_REGISTRY_UPSTREAM_URL", upstream_url)

        async with serve_app_in_loop(proxy_app) as proxy_url, httpx.AsyncClient(base_url=proxy_url) as client:
            raw_headers = [(b"accept", t.encode()) for t in DOCKER_ACCEPT_TYPES]
            response = await client.get("/v2/critic/manifests/sha256:abc123", headers=raw_headers)

    assert response.status_code == 200
    assert len(captured) == 1, f"Expected 1 upstream request, got {len(captured)}"

    # Extract all Accept values the upstream received
    upstream_accept_values = [v for k, v in captured[0] if k.lower() == "accept"]

    # All values may arrive as separate headers or comma-joined; check all types present
    all_accept = ", ".join(upstream_accept_values)
    for media_type in DOCKER_ACCEPT_TYPES:
        assert media_type in all_accept, (
            f"Accept type {media_type!r} was lost during proxying. Upstream received Accept: {upstream_accept_values!r}"
        )


async def test_proxy_forwards_streaming_mutation_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blob upload PATCH requests should stream through without losing body bytes."""
    captured_body = b""

    upstream_app = FastAPI()

    @upstream_app.api_route("/{path:path}", methods=["PATCH"])
    async def catch_all(request: Request) -> Response:
        nonlocal captured_body
        chunks = [chunk async for chunk in request.stream()]
        captured_body = b"".join(chunks)
        return Response(status_code=202)

    proxy_app = FastAPI()

    @proxy_app.api_route("/{path:path}", methods=["PATCH"])
    async def proxy(request: Request) -> Response:
        return await _proxy_to_upstream(request)

    async def body_chunks():
        yield b"first-"
        await asyncio.sleep(0)
        yield b"second"

    async with serve_app_in_loop(upstream_app) as upstream_url:
        monkeypatch.setenv("PROPS_REGISTRY_UPSTREAM_URL", upstream_url)

        async with serve_app_in_loop(proxy_app) as proxy_url, httpx.AsyncClient(base_url=proxy_url) as client:
            response = await client.patch("/v2/critic/blobs/uploads/session", content=body_chunks())

    assert response.status_code == 202
    assert captured_body == b"first-second"


async def test_concurrent_pushes_of_same_manifest_keep_first_record(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_fetch_started = asyncio.Event()
    release_first_fetch = asyncio.Event()
    fetch_count = 0

    async def extract_metadata(manifest_body: bytes, repository: str) -> routes._ImageMetadata:
        nonlocal fetch_count
        fetch_count += 1
        if fetch_count == 1:
            first_fetch_started.set()
            await release_first_fetch.wait()
            return routes._ImageMetadata(base_digest=None, display_name="first")
        return routes._ImageMetadata(base_digest=None, display_name="second")

    monkeypatch.setattr(routes, "_extract_image_metadata", extract_metadata)
    digest = "sha256:" + "a" * 64
    async with AsyncDatabase(db.config) as async_db:
        first_push = asyncio.create_task(routes._record_manifest_push("critic", digest, b"{}", None, async_db))
        await first_fetch_started.wait()
        try:
            await routes._record_manifest_push("critic", digest, b"{}", None, async_db)
        finally:
            release_first_fetch.set()
        await first_push

    with db.session() as session:
        definition = session.get(AgentDefinition, digest)
        assert definition is not None
        assert definition.display_name == "second"


def _basic_auth(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


async def test_registry_postgres_auth_and_acl(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """The standalone proxy resolves real PostgreSQL roles before applying OCI ACLs."""
    monkeypatch.setenv("PROPS_AGENT_PASSWORD_SALT", "registry-test-salt")
    critic_dev = await make_agent_credentials(
        db,
        CriticDevOptimizeTypeConfig(target_metric=TargetMetric.WHOLE_REPO, optimizer_model="test", critic_model="test"),
        FAKE_CRITIC_DEV_OPTIMIZE_DIGEST,
    )
    freeform = await make_agent_credentials(db, FreeformTypeConfig(), FAKE_CRITIC_DIGEST)

    # The evaluator role is created by migrations without a password unless the
    # environment supplies one. Give it a test-only password in this container.
    with db.session() as session:
        session.execute(text("ALTER ROLE evaluator PASSWORD 'registry-test-evaluator'"))

    async def fake_upstream(request: Request) -> Response:
        return Response(status_code=201 if request.method == "PUT" else 200)

    monkeypatch.setattr(routes, "_proxy_to_upstream", fake_upstream)

    async def fake_metadata(body: bytes, repo: str) -> routes._ImageMetadata:
        return routes._ImageMetadata(base_digest=None, display_name="test")

    monkeypatch.setattr(routes, "_extract_image_metadata", fake_metadata)

    async with AsyncDatabase(db.config) as async_db:
        with TestClient(create_app(db=async_db)) as client:
            path = "/v2/critic/blobs/sha256:abc"
            assert client.get("/v2/").status_code == 401
            assert client.get(path, headers=_basic_auth(db.config.user, db.config.password)).status_code == 200
            assert client.get(path, headers=_basic_auth("evaluator", "registry-test-evaluator")).status_code == 200
            assert client.get(path, headers=_basic_auth(critic_dev.username, critic_dev.password)).status_code == 200
            assert client.get(path, headers=_basic_auth(freeform.username, freeform.password)).status_code == 403
            assert client.get(path, headers=_basic_auth(critic_dev.username, "wrong")).status_code == 401

            digest_ref = "sha256:" + "c" * 64
            manifest_path = f"/v2/critic/manifests/{digest_ref}"
            assert (
                client.put(manifest_path, headers=_basic_auth(critic_dev.username, critic_dev.password)).status_code
                == 201
            )
            assert (
                client.put(
                    "/v2/critic/manifests/latest", headers=_basic_auth(critic_dev.username, critic_dev.password)
                ).status_code
                == 403
            )
            assert (
                client.put(
                    "/v2/critic/manifests/latest", headers=_basic_auth("evaluator", "registry-test-evaluator")
                ).status_code
                == 201
            )

    with db.session() as session:
        definition = session.get(AgentDefinition, "sha256:" + hashlib.sha256(b"").hexdigest())
        assert definition is not None
        assert definition.created_by_agent_run_id is not None


def test_only_grader_builtin_tag_move_notifies_supervisor() -> None:
    """The GraderSupervisor restart is triggered ONLY by a move of the grader's
    builtin tag. By-digest pushes and per-commit sha pins (pushed alongside
    `latest` by //props/agents:push_images_bin) must not trigger it — otherwise
    every devel commit restarts every grader, cancelling in-flight grades."""
    grader = str(AgentType.GRADER)
    # The one case that should notify: grader builtin tag moved.
    assert _is_grader_builtin_push(grader, BUILTIN_TAG)
    # Per-commit pinned sha tag pushed next to latest — must NOT notify.
    assert not _is_grader_builtin_push(grader, "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2")
    # By-digest push (crane push @sha256:...) — must NOT notify.
    assert not _is_grader_builtin_push(grader, "sha256:" + "ab" * 32)
    # Other repos never drive the grader supervisor, even on their builtin tag.
    assert not _is_grader_builtin_push(str(AgentType.CRITIC), BUILTIN_TAG)


if __name__ == "__main__":
    pytest_bazel.main()
