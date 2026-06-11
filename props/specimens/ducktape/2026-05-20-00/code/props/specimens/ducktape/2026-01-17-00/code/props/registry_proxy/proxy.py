"""OCI Registry proxy with ACL enforcement and metadata tracking.

Sits between agents and the upstream registry to:
- Validate agent auth tokens against postgres
- Enforce ACL based on agent type (admin/PO/PI/critic/grader)
- Record image refs in database when pushed
- Prevent unauthorized operations

The proxy implements the OCI Distribution API, forwarding valid requests
to the upstream registry while enforcing access controls.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated
from uuid import UUID

import httpx
import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from sqlalchemy.orm import Session

from props.core.db.models import AgentDefinition, AgentRun, AgentType
from props.core.db.session import get_session
from props.core.oci_utils import is_digest

logger = logging.getLogger(__name__)

# Environment variables for registry and postgres configuration
UPSTREAM_REGISTRY_URL = os.environ.get("PROPS_REGISTRY_UPSTREAM_URL", "http://props-registry:5000")
PGHOST = os.environ.get("PGHOST", "props-postgres")
PGPORT = os.environ.get("PGPORT", "5432")
PGDATABASE = os.environ.get("PGDATABASE", "eval_results")


class CallerType(StrEnum):
    """Type of caller accessing the registry."""

    ANONYMOUS = "anonymous"  # No auth - only /v2/ endpoint allowed
    ADMIN = "admin"  # postgres user - full access
    PROMPT_OPTIMIZER = "prompt-optimizer"  # PO agent - can read/push
    PROMPT_IMPROVER = "prompt-improver"  # PI agent - can read/push
    CRITIC = "critic"  # Critic agent - no registry access
    GRADER = "grader"  # Grader agent - no registry access
    UNKNOWN = "unknown"  # Invalid/unrecognized caller


@dataclass
class AuthContext:
    """Authenticated caller context."""

    caller_type: CallerType
    agent_run_id: UUID | None  # None for admin


def _validate_postgres_credentials(username: str, password: str) -> bool:
    """Validate credentials by attempting postgres connection.

    Returns True if credentials are valid, False otherwise.
    """
    try:
        # Attempt connection with provided credentials
        with psycopg.connect(
            host=PGHOST, port=PGPORT, dbname=PGDATABASE, user=username, password=password, connect_timeout=5
        ):
            return True
    except psycopg.OperationalError:
        return False


def _parse_auth_header(authorization: str | None) -> AuthContext | None:
    """Parse authorization header and determine caller type.

    Supports Basic auth for both admin and agents (validates against postgres).
    - Admin: Basic auth with postgres admin username
    - Agents: Basic auth with agent_{run_id} username

    Returns None if auth is invalid.
    """
    if not authorization:
        return None

    # Basic auth for both admin and agents
    if authorization.startswith("Basic "):
        try:
            encoded = authorization.removeprefix("Basic ")
            decoded = base64.b64decode(encoded).decode("utf-8")
            username, password = decoded.split(":", 1)

            # Validate credentials against postgres
            if not _validate_postgres_credentials(username, password):
                logger.warning(f"Invalid postgres credentials for user: {username}")
                return None

            # Determine caller type based on username pattern
            if username.startswith("agent_"):
                # Agent user: extract run_id from username
                try:
                    agent_run_id = UUID(username.removeprefix("agent_"))
                    return AuthContext(caller_type=CallerType.UNKNOWN, agent_run_id=agent_run_id)
                except ValueError:
                    logger.warning(f"Invalid UUID in agent username: {username}")
                    return None
            else:
                # Admin user
                return AuthContext(caller_type=CallerType.ADMIN, agent_run_id=None)

        except (ValueError, UnicodeDecodeError) as e:
            logger.warning(f"Failed to parse Basic auth: {e}")
            return None

    return None


def get_auth(authorization: Annotated[str | None, Header()] = None) -> AuthContext:
    """Dependency to extract and validate caller auth.

    For agents: verifies agent run exists and determines agent type.
    For admin: validates basic auth credentials.
    No auth: returns anonymous context (only /v2/ allowed).
    """
    if authorization is None:
        # Allow anonymous access - permission check will restrict to /v2/ only
        return AuthContext(caller_type=CallerType.ANONYMOUS, agent_run_id=None)

    auth = _parse_auth_header(authorization)
    if auth is None:
        raise HTTPException(status_code=401, detail="Invalid authorization")

    # Admin doesn't need further validation (credentials will be checked by postgres)
    if auth.caller_type == CallerType.ADMIN:
        return auth

    # For agents, look up run in database to determine type
    assert auth.agent_run_id is not None
    with get_session() as session:
        agent_run = session.get(AgentRun, auth.agent_run_id)
        if agent_run is None:
            raise HTTPException(status_code=401, detail="Invalid agent token")

        # Extract agent type from type_config (now a Pydantic model)
        agent_type = agent_run.type_config.agent_type

        caller_type_map = {
            AgentType.PROMPT_OPTIMIZER: CallerType.PROMPT_OPTIMIZER,
            AgentType.IMPROVEMENT: CallerType.PROMPT_IMPROVER,
            AgentType.CRITIC: CallerType.CRITIC,
            AgentType.GRADER: CallerType.GRADER,
        }

        auth.caller_type = caller_type_map.get(agent_type, CallerType.UNKNOWN)

    return auth


# ACL: sets of caller types allowed for each operation
CAN_READ = {CallerType.ADMIN, CallerType.PROMPT_OPTIMIZER, CallerType.PROMPT_IMPROVER}
CAN_PUSH = {CallerType.ADMIN, CallerType.PROMPT_OPTIMIZER, CallerType.PROMPT_IMPROVER}
CAN_PUSH_TAGS = {CallerType.ADMIN}  # Only admin can push by tag


def _check_permission(auth: AuthContext, operation: str, path: str, method: str) -> None:
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
            if auth.caller_type not in CAN_READ:
                raise HTTPException(status_code=403, detail=f"{auth.caller_type} not allowed to read")
            return

        # Tag list: /v2/<repo>/tags/list
        if re.fullmatch(r"v2/[^/]+/tags/list", path):
            if auth.caller_type not in CAN_READ:
                raise HTTPException(status_code=403, detail=f"{auth.caller_type} not allowed to read")
            return

        # Manifest read: /v2/<repo>/manifests/<ref>
        if re.fullmatch(r"v2/[^/]+/manifests/[^/]+", path):
            if auth.caller_type not in CAN_READ:
                raise HTTPException(status_code=403, detail=f"{auth.caller_type} not allowed to read")
            return

        # Blob read: /v2/<repo>/blobs/<digest>
        if re.fullmatch(r"v2/[^/]+/blobs/[^/]+", path):
            if auth.caller_type not in CAN_READ:
                raise HTTPException(status_code=403, detail=f"{auth.caller_type} not allowed to read")
            return

        # Unrecognized read operation - deny
        raise HTTPException(status_code=403, detail=f"Unrecognized read operation: {method} {path}")

    # Manifest push: PUT /v2/<repo>/manifests/<ref>
    if method == "PUT":
        match = re.fullmatch(r"v2/([^/]+)/manifests/([^/]+)", path)
        if match:
            if auth.caller_type not in CAN_PUSH:
                raise HTTPException(status_code=403, detail=f"{auth.caller_type} not allowed to push")
            # Check if pushing by tag (requires additional permission)
            ref = match.group(2)
            if not is_digest(ref) and auth.caller_type not in CAN_PUSH_TAGS:
                raise HTTPException(status_code=403, detail=f"{auth.caller_type} not allowed to push by tag")
            return

    # Blob upload operations: POST to start upload, PATCH/PUT to continue/complete
    if method in ("POST", "PATCH", "PUT"):
        # POST /v2/<repo>/blobs/uploads/ - start upload
        if method == "POST" and re.fullmatch(r"v2/[^/]+/blobs/uploads/?", path):
            if auth.caller_type not in CAN_PUSH:
                raise HTTPException(status_code=403, detail=f"{auth.caller_type} not allowed to push")
            return

        # PATCH /v2/<repo>/blobs/uploads/<uuid> - continue upload
        if method == "PATCH" and re.fullmatch(r"v2/[^/]+/blobs/uploads/[^/]+", path):
            if auth.caller_type not in CAN_PUSH:
                raise HTTPException(status_code=403, detail=f"{auth.caller_type} not allowed to push")
            return

        # PUT /v2/<repo>/blobs/uploads/<uuid>?digest=... - complete upload
        # Note: query params are in request.url.query, not path, so just validate path structure
        if method == "PUT" and re.fullmatch(r"v2/[^/]+/blobs/uploads/[^/]+", path):
            if auth.caller_type not in CAN_PUSH:
                raise HTTPException(status_code=403, detail=f"{auth.caller_type} not allowed to push")
            return

    # Default: deny any unrecognized operations
    raise HTTPException(status_code=403, detail=f"Operation not allowed: {method} {path}")


async def _extract_base_digest(manifest_body: bytes, repository: str) -> str | None:
    """Extract base image digest from OCI manifest.

    Attempts to determine the parent/base image digest by:
    1. Fetching the image config blob from the manifest
    2. Looking for org.opencontainers.image.base.digest annotation
    3. Returning None if not found or on error

    Args:
        manifest_body: Raw manifest JSON bytes
        repository: Repository name for fetching config blob

    Returns:
        Base image digest (sha256:...) if found, None otherwise
    """
    try:
        manifest = json.loads(manifest_body)

        # Extract config digest from manifest
        config_descriptor = manifest.get("config")
        if not config_descriptor:
            return None

        config_digest = config_descriptor.get("digest")
        if not config_digest:
            return None

        # Fetch config blob from upstream registry
        config_url = f"{UPSTREAM_REGISTRY_URL}/v2/{repository}/blobs/{config_digest}"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(config_url, timeout=5.0)
                if response.status_code != 200:
                    logger.warning(f"Failed to fetch config blob {config_digest}: HTTP {response.status_code}")
                    return None

                config = response.json()

                # Look for base image digest in annotations
                # Standard OCI annotation: org.opencontainers.image.base.digest
                config_annotations = config.get("config", {}).get("Labels", {})
                base_digest: str | None = config_annotations.get("org.opencontainers.image.base.digest")

                if base_digest:
                    logger.info(f"Extracted base_digest from annotation: {base_digest}")
                    return base_digest

                return None

            except (httpx.RequestError, json.JSONDecodeError) as e:
                logger.warning(f"Error fetching/parsing config blob: {e}")
                return None

    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Error parsing manifest for base_digest extraction: {e}")
        return None


async def _record_manifest_push(
    session: Session, repository: str, digest: str, manifest_body: bytes, auth: AuthContext
) -> None:
    """Record a manifest push to agent_definitions table.

    Args:
        session: Database session
        repository: Repository name (e.g., "critic")
        digest: Manifest digest (sha256:...)
        manifest_body: Raw manifest JSON bytes
        auth: Caller authentication context
    """
    # Map repository name to agent_type enum (repository names match enum values)
    try:
        agent_type = AgentType(repository)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown repository name: {repository}. Must be a valid agent type: {[t.value for t in AgentType]}",
        )

    # Check if definition already exists (idempotent)
    existing = session.get(AgentDefinition, digest)
    if existing:
        logger.info(f"Agent definition {digest} already exists, skipping")
        return

    # Extract base image digest from manifest
    base_digest = await _extract_base_digest(manifest_body, repository)

    # Create new agent definition
    definition = AgentDefinition(
        digest=digest,
        agent_type=agent_type,
        created_by_agent_run_id=auth.agent_run_id,  # None for admin pushes
        base_digest=base_digest,
    )

    session.add(definition)
    session.commit()

    logger.info(
        f"Recorded agent definition: {repository}@{digest} "
        f"(type={agent_type}, created_by={auth.agent_run_id}, base={base_digest or 'none'})"
    )


# FastAPI app
app = FastAPI(title="Props Registry Proxy")


@app.get("/health")
async def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}


@app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(request: Request, path: str, auth: Annotated[AuthContext, Depends(get_auth)]) -> Response:
    """Proxy all OCI registry requests with ACL enforcement."""
    # Check permissions
    _check_permission(auth, "proxy", path, request.method)

    # Build upstream URL
    upstream_url = f"{UPSTREAM_REGISTRY_URL}/{path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    # Forward request to upstream registry
    async with httpx.AsyncClient() as client:
        # Prepare request
        headers = dict(request.headers)
        # Remove host header (will be set by httpx)
        headers.pop("host", None)
        body = await request.body()

        # Special handling for manifest pushes: record in database
        # Validate path structure: PUT /v2/<repo>/manifests/<ref>
        manifest_push_match = None
        if request.method == "PUT":
            manifest_push_match = re.fullmatch(r"v2/([^/]+)/manifests/([^/]+)", path)

        manifest_digest = None
        if manifest_push_match:
            # Compute manifest digest from body
            manifest_digest = f"sha256:{hashlib.sha256(body).hexdigest()}"

        # Forward request
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
            assert manifest_digest is not None  # Set on line 387 when manifest_push_match is truthy
            with get_session() as session:
                await _record_manifest_push(session, repository, manifest_digest, body, auth)

        # Return upstream response
        return Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            headers=dict(upstream_response.headers),
        )
