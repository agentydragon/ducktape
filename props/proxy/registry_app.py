"""OCI Registry Proxy App - ACL enforcement and metadata tracking.

This is a self-contained FastAPI application that can run standalone or be
mounted into a larger application.

Endpoints:
- GET /health - Health check
- /{path:path} - OCI Distribution API proxy (GET, HEAD, POST, PUT, PATCH, DELETE)

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
import re
from enum import StrEnum
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request, Response

from props.core.oci_utils import is_digest
from props.db.models import AgentDefinition, AgentRun, AgentType
from props.db.session import get_session
from props.proxy.auth import AuthContext, AuthMiddleware, get_auth_context

logger = logging.getLogger(__name__)

# Environment variables for registry configuration
UPSTREAM_REGISTRY_URL = os.environ.get("PROPS_REGISTRY_UPSTREAM_URL", "http://props-registry:5000")


class CallerType(StrEnum):
    """Type of caller accessing the registry."""

    ANONYMOUS = "anonymous"  # No auth - only /v2/ endpoint allowed
    ADMIN = "admin"  # postgres user - full access
    PROMPT_OPTIMIZER = "prompt-optimizer"  # PO agent - can read/push
    PROMPT_IMPROVER = "prompt-improver"  # PI agent - can read/push
    CRITIC = "critic"  # Critic agent - no registry access
    GRADER = "grader"  # Grader agent - no registry access
    UNKNOWN = "unknown"  # Invalid/unrecognized caller


def _get_caller_type(auth: AuthContext) -> tuple[CallerType, UUID | None]:
    """Determine caller type from auth context.

    Returns (caller_type, agent_run_id).
    """
    if auth.error:
        raise HTTPException(status_code=401, detail=auth.error)

    if not auth.is_authenticated:
        return CallerType.ANONYMOUS, None

    if auth.is_admin:
        return CallerType.ADMIN, None

    # For agents, look up run in database to determine type
    assert auth.agent_run_id is not None
    with get_session() as session:
        agent_run = session.get(AgentRun, auth.agent_run_id)
        if agent_run is None:
            raise HTTPException(status_code=401, detail="Invalid agent token")

        agent_type = agent_run.type_config.agent_type

        caller_type_map = {
            AgentType.PROMPT_OPTIMIZER: CallerType.PROMPT_OPTIMIZER,
            AgentType.IMPROVEMENT: CallerType.PROMPT_IMPROVER,
            AgentType.CRITIC: CallerType.CRITIC,
            AgentType.GRADER: CallerType.GRADER,
        }

        return caller_type_map.get(agent_type, CallerType.UNKNOWN), auth.agent_run_id


# ACL: sets of caller types allowed for each operation
CAN_READ = {CallerType.ADMIN, CallerType.PROMPT_OPTIMIZER, CallerType.PROMPT_IMPROVER}
CAN_PUSH = {CallerType.ADMIN, CallerType.PROMPT_OPTIMIZER, CallerType.PROMPT_IMPROVER}
CAN_PUSH_TAGS = {CallerType.ADMIN}  # Only admin can push by tag


def _check_permission(caller_type: CallerType, path: str, method: str) -> None:
    """Check if caller has permission for this operation.

    Uses default-deny with explicit path validation using regex patterns.
    Raises HTTPException if permission denied.
    """
    # Delete always forbidden
    if method == "DELETE":
        raise HTTPException(status_code=403, detail="DELETE operations are forbidden")

    # API version check (GET /v2/) - allow all callers
    if method in {"GET", "HEAD"} and re.fullmatch(r"v2/?", path):
        return

    # Read operations: validate full path structure
    if method in {"GET", "HEAD"}:
        # Catalog endpoint: /v2/_catalog
        if re.fullmatch(r"v2/_catalog", path):
            if caller_type not in CAN_READ:
                raise HTTPException(status_code=403, detail=f"{caller_type} not allowed to read")
            return

        # Tag list: /v2/<repo>/tags/list
        if re.fullmatch(r"v2/[^/]+/tags/list", path):
            if caller_type not in CAN_READ:
                raise HTTPException(status_code=403, detail=f"{caller_type} not allowed to read")
            return

        # Manifest read: /v2/<repo>/manifests/<ref>
        if re.fullmatch(r"v2/[^/]+/manifests/[^/]+", path):
            if caller_type not in CAN_READ:
                raise HTTPException(status_code=403, detail=f"{caller_type} not allowed to read")
            return

        # Blob read: /v2/<repo>/blobs/<digest>
        if re.fullmatch(r"v2/[^/]+/blobs/[^/]+", path):
            if caller_type not in CAN_READ:
                raise HTTPException(status_code=403, detail=f"{caller_type} not allowed to read")
            return

        # Unrecognized read operation - deny
        raise HTTPException(status_code=403, detail=f"Unrecognized read operation: {method} {path}")

    # Manifest push: PUT /v2/<repo>/manifests/<ref>
    if method == "PUT":
        match = re.fullmatch(r"v2/([^/]+)/manifests/([^/]+)", path)
        if match:
            if caller_type not in CAN_PUSH:
                raise HTTPException(status_code=403, detail=f"{caller_type} not allowed to push")
            # Check if pushing by tag (requires additional permission)
            ref = match.group(2)
            if not is_digest(ref) and caller_type not in CAN_PUSH_TAGS:
                raise HTTPException(status_code=403, detail=f"{caller_type} not allowed to push by tag")
            return

    # Blob upload operations: POST to start upload, PATCH/PUT to continue/complete
    if method in ("POST", "PATCH", "PUT"):
        # POST /v2/<repo>/blobs/uploads/ - start upload
        if method == "POST" and re.fullmatch(r"v2/[^/]+/blobs/uploads/?", path):
            if caller_type not in CAN_PUSH:
                raise HTTPException(status_code=403, detail=f"{caller_type} not allowed to push")
            return

        # PATCH /v2/<repo>/blobs/uploads/<uuid> - continue upload
        if method == "PATCH" and re.fullmatch(r"v2/[^/]+/blobs/uploads/[^/]+", path):
            if caller_type not in CAN_PUSH:
                raise HTTPException(status_code=403, detail=f"{caller_type} not allowed to push")
            return

        # PUT /v2/<repo>/blobs/uploads/<uuid>?digest=... - complete upload
        if method == "PUT" and re.fullmatch(r"v2/[^/]+/blobs/uploads/[^/]+", path):
            if caller_type not in CAN_PUSH:
                raise HTTPException(status_code=403, detail=f"{caller_type} not allowed to push")
            return

    # Default: deny any unrecognized operations
    raise HTTPException(status_code=403, detail=f"Operation not allowed: {method} {path}")


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


# Create standalone FastAPI app
app = FastAPI(title="Props Registry Proxy")

# Add auth middleware
app.add_middleware(AuthMiddleware)


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(request: Request, path: str) -> Response:
    """Proxy all OCI registry requests with ACL enforcement."""
    # Get auth context and determine caller type
    auth = get_auth_context(request)
    caller_type, agent_run_id = _get_caller_type(auth)

    # Check permissions
    _check_permission(caller_type, path, request.method)

    # Build upstream URL
    upstream_url = f"{UPSTREAM_REGISTRY_URL}/{path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    # Forward request to upstream registry
    async with httpx.AsyncClient() as client:
        headers = dict(request.headers)
        headers.pop("host", None)
        body = await request.body()

        # Special handling for manifest pushes
        manifest_push_match = None
        if request.method == "PUT":
            manifest_push_match = re.fullmatch(r"v2/([^/]+)/manifests/([^/]+)", path)

        manifest_digest = None
        if manifest_push_match:
            manifest_digest = f"sha256:{hashlib.sha256(body).hexdigest()}"

        try:
            upstream_response = await client.request(
                method=request.method, url=upstream_url, headers=headers, content=body, timeout=30.0
            )
        except httpx.RequestError as e:
            logger.error(f"Upstream request failed: {e}")
            raise HTTPException(status_code=502, detail=f"Upstream error: {e}")

        # Record manifest push if successful
        if manifest_push_match and upstream_response.status_code in (200, 201):
            repository = manifest_push_match.group(1)
            assert manifest_digest is not None
            await _record_manifest_push(repository, manifest_digest, body, agent_run_id)

        return Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            headers=dict(upstream_response.headers),
        )
