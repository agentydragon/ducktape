"""OCI Registry Proxy routes - ACL enforcement and metadata tracking.

Endpoints (OCI Distribution API):
- GET /v2/ - API version check (anonymous allowed)
- GET /v2/_catalog - List repositories
- GET /v2/{repo}/tags/list - List tags
- GET, HEAD /v2/{repo}/manifests/{ref} - Get manifest
- PUT /v2/{repo}/manifests/{ref} - Push manifest
- GET, HEAD /v2/{repo}/blobs/{digest} - Get blob
- POST /v2/{repo}/blobs/uploads/ - Start blob upload
- PATCH /v2/{repo}/blobs/uploads/{uuid} - Continue blob upload
- PUT /v2/{repo}/blobs/uploads/{uuid} - Complete blob upload

Features:
- Validates agent auth tokens against postgres
- Enforces ACL based on agent type (admin/PO/PI/critic/grader)
- Records image refs in database when pushed
- Prevents unauthorized operations
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from props.backend.auth import ACL_CAN_PUSH_TAGS, CallerType, require_registry_push, require_registry_read
from props.core.oci_utils import is_digest
from props.db.models import AgentDefinition, AgentType
from props.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter()

# Environment variables for registry configuration
UPSTREAM_REGISTRY_URL = os.environ.get("PROPS_REGISTRY_UPSTREAM_URL", "http://props-registry:5000")


# =============================================================================
# Helper functions
# =============================================================================


def _require_push_tag(caller_type: CallerType, ref: str) -> None:
    """Require push-by-tag permission if ref is a tag, raise HTTPException if denied."""
    if not is_digest(ref) and caller_type not in ACL_CAN_PUSH_TAGS:
        raise HTTPException(status_code=403, detail=f"{caller_type} not allowed to push by tag")


async def _proxy_to_upstream(request: Request, path: str) -> Response:
    """Forward request to upstream registry and return response."""
    upstream_url = f"{UPSTREAM_REGISTRY_URL}/{path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    async with httpx.AsyncClient() as client:
        headers = dict(request.headers)
        headers.pop("host", None)
        body = await request.body()

        try:
            upstream_response = await client.request(
                method=request.method, url=upstream_url, headers=headers, content=body, timeout=30.0
            )
        except httpx.RequestError as e:
            logger.error(f"Upstream request failed: {e}")
            raise HTTPException(status_code=502, detail=f"Upstream error: {e}")

        return Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            headers=dict(upstream_response.headers),
        )


async def _extract_base_digest(manifest_body: bytes, repository: str) -> str | None:
    """Extract base image digest from OCI manifest."""
    try:
        manifest = json.loads(manifest_body)
        config_descriptor = manifest.get("config")
        if not config_descriptor:
            return None

        config_digest = config_descriptor.get("digest")
        if not config_digest:
            return None

        config_url = f"{UPSTREAM_REGISTRY_URL}/v2/{repository}/blobs/{config_digest}"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(config_url, timeout=5.0)
                if response.status_code != 200:
                    return None

                config = response.json()
                config_annotations = config.get("config", {}).get("Labels", {})
                base_digest: str | None = config_annotations.get("org.opencontainers.image.base.digest")

                if base_digest:
                    logger.info(f"Extracted base_digest from annotation: {base_digest}")
                return base_digest

            except (httpx.RequestError, json.JSONDecodeError) as e:
                logger.warning(f"Error fetching/parsing config blob: {e}")
                return None

    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Error parsing manifest for base_digest extraction: {e}")
        return None


async def _record_manifest_push(repository: str, digest: str, manifest_body: bytes, agent_run_id: UUID | None) -> None:
    """Record a manifest push to agent_definitions table."""
    try:
        agent_type = AgentType(repository)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown repository name: {repository}. Must be a valid agent type: {[t.value for t in AgentType]}",
        )

    with get_session() as session:
        existing = session.get(AgentDefinition, digest)
        if existing:
            logger.info(f"Agent definition {digest} already exists, skipping")
            return

        base_digest = await _extract_base_digest(manifest_body, repository)
        definition = AgentDefinition(
            digest=digest, agent_type=agent_type, created_by_agent_run_id=agent_run_id, base_digest=base_digest
        )
        session.add(definition)
        session.commit()

        logger.info(
            f"Recorded agent definition: {repository}@{digest} "
            f"(type={agent_type}, created_by={agent_run_id}, base={base_digest or 'none'})"
        )


# =============================================================================
# OCI Distribution API v2 routes
# =============================================================================


@router.get("/v2/")
@router.head("/v2/")
async def v2_check() -> Response:
    """API version check - allows anonymous access per OCI spec."""
    return Response(content=b"{}", status_code=200, headers={"Docker-Distribution-API-Version": "registry/2.0"})


@router.get("/v2/_catalog")
async def get_catalog(
    request: Request, auth: tuple[CallerType, UUID | None] = Depends(require_registry_read)
) -> Response:
    """List repositories."""
    return await _proxy_to_upstream(request, "v2/_catalog")


@router.get("/v2/{repo}/tags/list")
async def get_tags(
    request: Request, repo: str, auth: tuple[CallerType, UUID | None] = Depends(require_registry_read)
) -> Response:
    """List tags for a repository."""
    return await _proxy_to_upstream(request, f"v2/{repo}/tags/list")


@router.get("/v2/{repo}/manifests/{ref}")
@router.head("/v2/{repo}/manifests/{ref}")
async def get_manifest(
    request: Request, repo: str, ref: str, auth: tuple[CallerType, UUID | None] = Depends(require_registry_read)
) -> Response:
    """Get a manifest by tag or digest."""
    return await _proxy_to_upstream(request, f"v2/{repo}/manifests/{ref}")


@router.put("/v2/{repo}/manifests/{ref}")
async def put_manifest(
    request: Request, repo: str, ref: str, auth: tuple[CallerType, UUID | None] = Depends(require_registry_push)
) -> Response:
    """Push a manifest."""
    caller_type, agent_run_id = auth
    _require_push_tag(caller_type, ref)

    # Read body for digest computation and recording
    body = await request.body()
    manifest_digest = f"sha256:{hashlib.sha256(body).hexdigest()}"

    # Forward to upstream
    upstream_url = f"{UPSTREAM_REGISTRY_URL}/v2/{repo}/manifests/{ref}"
    async with httpx.AsyncClient() as client:
        headers = dict(request.headers)
        headers.pop("host", None)

        try:
            upstream_response = await client.put(upstream_url, headers=headers, content=body, timeout=30.0)
        except httpx.RequestError as e:
            logger.error(f"Upstream request failed: {e}")
            raise HTTPException(status_code=502, detail=f"Upstream error: {e}")

        # Record manifest push if successful
        if upstream_response.status_code in (200, 201):
            await _record_manifest_push(repo, manifest_digest, body, agent_run_id)

        return Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            headers=dict(upstream_response.headers),
        )


@router.get("/v2/{repo}/blobs/{digest}")
@router.head("/v2/{repo}/blobs/{digest}")
async def get_blob(
    request: Request, repo: str, digest: str, auth: tuple[CallerType, UUID | None] = Depends(require_registry_read)
) -> Response:
    """Get a blob by digest."""
    return await _proxy_to_upstream(request, f"v2/{repo}/blobs/{digest}")


@router.post("/v2/{repo}/blobs/uploads/")
async def start_blob_upload(
    request: Request, repo: str, auth: tuple[CallerType, UUID | None] = Depends(require_registry_push)
) -> Response:
    """Start a blob upload."""
    return await _proxy_to_upstream(request, f"v2/{repo}/blobs/uploads/")


@router.patch("/v2/{repo}/blobs/uploads/{uuid}")
async def continue_blob_upload(
    request: Request, repo: str, uuid: str, auth: tuple[CallerType, UUID | None] = Depends(require_registry_push)
) -> Response:
    """Continue a blob upload (chunked)."""
    return await _proxy_to_upstream(request, f"v2/{repo}/blobs/uploads/{uuid}")


@router.put("/v2/{repo}/blobs/uploads/{uuid}")
async def complete_blob_upload(
    request: Request, repo: str, uuid: str, auth: tuple[CallerType, UUID | None] = Depends(require_registry_push)
) -> Response:
    """Complete a blob upload."""
    return await _proxy_to_upstream(request, f"v2/{repo}/blobs/uploads/{uuid}")
