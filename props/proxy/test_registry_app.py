"""Tests for Registry proxy router.

Tests cover:
- Anonymous access to /v2/ endpoint
- ACL enforcement for different caller types
- Permission checks for read/push operations
- Manifest push recording
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_bazel
from starlette.testclient import TestClient

from props.db.config import DatabaseConfig
from props.proxy.conftest import basic_auth_header

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


class TestRegistryAnonymousAccess:
    """Tests for anonymous access to registry endpoints."""

    @patch("props.proxy.registry_app.httpx.AsyncClient")
    async def test_v2_endpoint_allows_anonymous(self, mock_client_class: AsyncMock, client: TestClient):
        """GET /v2/ endpoint allows anonymous access."""
        # Mock upstream registry response
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.content = b"{}"
        mock_response.headers = {"content-type": "application/json"}

        mock_client = AsyncMock()
        mock_client.request.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        response = client.get("/v2/")
        # Should succeed (200) or at least not be auth error (401/403)
        assert response.status_code == 200

    def test_catalog_endpoint_requires_auth(self, client: TestClient):
        """GET /v2/_catalog endpoint requires authentication."""
        response = client.get("/v2/_catalog")
        assert response.status_code == 403
        assert "anonymous" in response.json()["detail"].lower()

    def test_manifest_read_requires_auth(self, client: TestClient):
        """GET /v2/<repo>/manifests/<ref> requires authentication."""
        response = client.get("/v2/critic/manifests/latest")
        assert response.status_code == 403
        assert "anonymous" in response.json()["detail"].lower()

    def test_blob_read_requires_auth(self, client: TestClient):
        """GET /v2/<repo>/blobs/<digest> requires authentication."""
        response = client.get("/v2/critic/blobs/sha256:abc123")
        assert response.status_code == 403
        assert "anonymous" in response.json()["detail"].lower()


class TestRegistryAuthValidation:
    """Tests for registry authentication validation."""

    def test_invalid_credentials_returns_401(self, client: TestClient, synced_test_db: DatabaseConfig):
        """Request with invalid credentials returns 401."""
        response = client.get(
            "/v2/_catalog",
            headers={"Authorization": basic_auth_header("agent_00000000-0000-0000-0000-000000000000", "wrong")},
        )
        assert response.status_code == 401

    def test_malformed_auth_returns_401(self, client: TestClient):
        """Request with malformed auth header returns 401."""
        response = client.get("/v2/_catalog", headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401


class TestRegistryACL:
    """Tests for registry ACL enforcement."""

    def test_delete_always_forbidden(self, client: TestClient, synced_test_db: DatabaseConfig):
        """DELETE operations are always forbidden."""
        # Even with valid auth, DELETE should be forbidden
        response = client.delete("/v2/critic/manifests/latest")
        assert response.status_code == 403
        assert "DELETE" in response.json()["detail"]

    def test_unrecognized_path_forbidden(self, client: TestClient, synced_test_db: DatabaseConfig):
        """Unrecognized paths are forbidden."""
        response = client.get("/v2/some/invalid/path/structure")
        assert response.status_code == 403


class TestRegistryReadOperations:
    """Tests for registry read operations with valid auth."""

    @patch("props.proxy.registry_app.httpx.AsyncClient")
    async def test_admin_can_read_catalog(
        self, mock_client_class: AsyncMock, client: TestClient, synced_test_db: DatabaseConfig
    ):
        """Admin user can read catalog."""
        # Mock upstream response
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.content = b'{"repositories": ["critic"]}'
        mock_response.headers = {"content-type": "application/json"}

        mock_client = AsyncMock()
        mock_client.request.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        # Use admin credentials from synced_test_db
        response = client.get(
            "/v2/_catalog",
            headers={"Authorization": basic_auth_header(synced_test_db.admin.user, synced_test_db.admin.password)},
        )
        assert response.status_code == 200

    @patch("props.proxy.registry_app.httpx.AsyncClient")
    async def test_admin_can_read_tags(
        self, mock_client_class: AsyncMock, client: TestClient, synced_test_db: DatabaseConfig
    ):
        """Admin user can read tag list."""
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.content = b'{"name": "critic", "tags": ["latest"]}'
        mock_response.headers = {"content-type": "application/json"}

        mock_client = AsyncMock()
        mock_client.request.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        response = client.get(
            "/v2/critic/tags/list",
            headers={"Authorization": basic_auth_header(synced_test_db.admin.user, synced_test_db.admin.password)},
        )
        assert response.status_code == 200

    @patch("props.proxy.registry_app.httpx.AsyncClient")
    async def test_admin_can_read_manifest(
        self, mock_client_class: AsyncMock, client: TestClient, synced_test_db: DatabaseConfig
    ):
        """Admin user can read manifests."""
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.content = b'{"schemaVersion": 2}'
        mock_response.headers = {"content-type": "application/vnd.oci.image.manifest.v1+json"}

        mock_client = AsyncMock()
        mock_client.request.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        response = client.get(
            "/v2/critic/manifests/sha256:abc123",
            headers={"Authorization": basic_auth_header(synced_test_db.admin.user, synced_test_db.admin.password)},
        )
        assert response.status_code == 200


class TestRegistryPushOperations:
    """Tests for registry push operations."""

    @patch("props.proxy.registry_app.httpx.AsyncClient")
    async def test_admin_can_start_blob_upload(
        self, mock_client_class: AsyncMock, client: TestClient, synced_test_db: DatabaseConfig
    ):
        """Admin user can start blob upload."""
        mock_response = AsyncMock()
        mock_response.status_code = 202
        mock_response.content = b""
        mock_response.headers = {"location": "/v2/critic/blobs/uploads/uuid-123"}

        mock_client = AsyncMock()
        mock_client.request.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        response = client.post(
            "/v2/critic/blobs/uploads/",
            headers={"Authorization": basic_auth_header(synced_test_db.admin.user, synced_test_db.admin.password)},
        )
        assert response.status_code == 202

    @patch("props.proxy.registry_app.httpx.AsyncClient")
    async def test_admin_can_push_manifest_by_digest(
        self, mock_client_class: AsyncMock, client: TestClient, synced_test_db: DatabaseConfig
    ):
        """Admin user can push manifest by digest."""
        manifest = b'{"schemaVersion": 2, "config": {"digest": "sha256:config123"}}'

        mock_response = AsyncMock()
        mock_response.status_code = 201
        mock_response.content = b""
        mock_response.headers = {"docker-content-digest": "sha256:manifest123"}

        mock_client = AsyncMock()
        mock_client.request.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        response = client.put(
            "/v2/critic/manifests/sha256:abc123",
            content=manifest,
            headers={
                "Authorization": basic_auth_header(synced_test_db.admin.user, synced_test_db.admin.password),
                "Content-Type": "application/vnd.oci.image.manifest.v1+json",
            },
        )
        assert response.status_code == 201

    @patch("props.proxy.registry_app.httpx.AsyncClient")
    async def test_admin_can_push_manifest_by_tag(
        self, mock_client_class: AsyncMock, client: TestClient, synced_test_db: DatabaseConfig
    ):
        """Admin user can push manifest by tag (only admin allowed)."""
        manifest = b'{"schemaVersion": 2, "config": {"digest": "sha256:config123"}}'

        mock_response = AsyncMock()
        mock_response.status_code = 201
        mock_response.content = b""
        mock_response.headers = {"docker-content-digest": "sha256:manifest123"}

        mock_client = AsyncMock()
        mock_client.request.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        response = client.put(
            "/v2/critic/manifests/latest",
            content=manifest,
            headers={
                "Authorization": basic_auth_header(synced_test_db.admin.user, synced_test_db.admin.password),
                "Content-Type": "application/vnd.oci.image.manifest.v1+json",
            },
        )
        assert response.status_code == 201


if __name__ == "__main__":
    pytest_bazel.main()
