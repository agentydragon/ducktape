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
from props.core.db.session import get_session_context
from props.core.oci_utils import is_digest

logger = logging.getLogger(__name__)

# Environment variables for registry and postgres configuration
UPSTREAM_REGISTRY_URL = os.environ.get("PROPS_REGISTRY_UPSTREAM_URL", "http://props-registry:5000")
PGHOST = os.environ.get("PGHOST", "props-postgres")
PGPORT = os.environ.get("PGPORT", "5432")
PGDATABASE = os.environ.get("PGDATABASE", "eval_results")


class CallerType(StrEnum):
    """Type of caller accessing the registry."""

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

    Supports:
    - Basic auth (validates against postgres)
    - Bearer token with agent_{run_id}_{secret} pattern (validates password against postgres)

    Returns None if auth is invalid.
    """
    if not authorization:
        return None

    # Basic auth for admin (postgres user)
    if authorization.startswith("Basic "):
        try:
            encoded = authorization.removeprefix("Basic ")
            decoded = base64.b64decode(encoded).decode("utf-8")
            username, password = decoded.split(":", 1)

            # Validate credentials against postgres
            if not _validate_postgres_credentials(username, password):
                logger.warning(f"Invalid postgres credentials for user: {username}")
                return None

            return AuthContext(caller_type=CallerType.ADMIN, agent_run_id=None)
        except (ValueError, UnicodeDecodeError) as e:
            logger.warning(f"Failed to parse Basic auth: {e}")
            return None

    # Bearer token for agents (format: agent_{agent_run_id}_{password})
    if authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")
        # Token format: agent_{agent_run_id}_{password}
        if not token.startswith("agent_"):
            return None

        parts = token.split("_", 2)
        if len(parts) < 3:
            logger.warning("Bearer token missing password component")
            return None

        try:
            agent_run_id = UUID(parts[1])
        except ValueError:
            logger.warning(f"Invalid UUID in agent token: {parts[1]}")
            return None

        # Validate agent credentials against postgres
        # Agent temp users have username format: agent_{run_id}
        username = f"agent_{agent_run_id}"
        password = parts[2]

        if not _validate_postgres_credentials(username, password):
            logger.warning(f"Invalid credentials for agent: {username}")
            return None

        return AuthContext(caller_type=CallerType.UNKNOWN, agent_run_id=agent_run_id)

    return None


def get_auth(authorization: Annotated[str | None, Header()] = None) -> AuthContext:
    """Dependency to extract and validate caller auth.

    For agents: verifies agent run exists and determines agent type.
    For admin: validates basic auth credentials.
    """
    auth = _parse_auth_header(authorization)
    if auth is None:
        raise HTTPException(status_code=401, detail="Invalid authorization")

    # Admin doesn't need further validation (credentials will be checked by postgres)
    if auth.caller_type == CallerType.ADMIN:
        return auth

    # For agents, look up run in database to determine type
    assert auth.agent_run_id is not None
    with get_session_context() as session:
        agent_run = session.get(AgentRun, auth.agent_run_id)
        if agent_run is None:
            raise HTTPException(status_code=401, detail="Invalid agent token")

        # Extract agent type from type_config JSONB
        agent_type_str = agent_run.type_config.get("agent_type")
        if not agent_type_str:
            raise HTTPException(status_code=500, detail="Agent run missing agent_type in type_config")

        # Map agent type to caller type
        try:
            agent_type = AgentType(agent_type_str)
        except ValueError:
            raise HTTPException(status_code=500, detail=f"Unknown agent type: {agent_type_str}")

        caller_type_map = {
            AgentType.PROMPT_OPTIMIZER: CallerType.PROMPT_OPTIMIZER,
            AgentType.PROMPT_IMPROVER: CallerType.PROMPT_IMPROVER,
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

    Raises HTTPException if permission denied.
    """
    # Read operations
    if method in {"GET", "HEAD"}:
        if auth.caller_type not in CAN_READ:
            raise HTTPException(status_code=403, detail=f"{auth.caller_type} not allowed to read")
        return

    # Manifest push
    if method == "PUT" and "/manifests/" in path:
        if auth.caller_type not in CAN_PUSH:
            raise HTTPException(status_code=403, detail=f"{auth.caller_type} not allowed to push")
        # Check if pushing by tag (requires additional permission)
        ref = path.split("/manifests/")[-1].split("?")[0]
        if not is_digest(ref) and auth.caller_type not in CAN_PUSH_TAGS:
            raise HTTPException(status_code=403, detail=f"{auth.caller_type} not allowed to push by tag")
        return

    # Blob upload operations (POST, PATCH, PUT to /blobs/)
    if "/blobs/" in path and method in ("POST", "PATCH", "PUT"):
        if auth.caller_type not in CAN_PUSH:
            raise HTTPException(status_code=403, detail=f"{auth.caller_type} not allowed to push")
        return

    # Delete always forbidden
    if method == "DELETE":
        raise HTTPException(status_code=403, detail=f"{auth.caller_type} not allowed to delete")

    # Default: allow (e.g., catalog, version check)


async def _record_manifest_push(session: Session, repository: str, digest: str, auth: AuthContext) -> None:
    """Record a manifest push to agent_definitions table.

    Args:
        session: Database session
        repository: Repository name (e.g., "critic")
        digest: Manifest digest (sha256:...)
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

    # Create new agent definition
    definition = AgentDefinition(
        digest=digest,
        agent_type=agent_type,
        created_by_agent_run_id=auth.agent_run_id,  # None for admin pushes
        base_digest=None,  # TODO: Extract from manifest if available
    )

    session.add(definition)
    session.commit()

    logger.info(f"Recorded agent definition: {repository}@{digest} (type={agent_type}, created_by={auth.agent_run_id})")


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
        is_manifest_push = request.method == "PUT" and "/v2/" in path and "/manifests/" in path
        manifest_digest = None

        if is_manifest_push:
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
        if is_manifest_push and upstream_response.status_code in (200, 201):
            # Extract repository name from path (/v2/<repo>/manifests/<ref>)
            match = re.match(r"^v2/([^/]+)/manifests/", path)
            if match:
                repository = match.group(1)
                with get_session_context() as session:
                    await _record_manifest_push(session, repository, manifest_digest, auth)

        # Return upstream response
        return Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            headers=dict(upstream_response.headers),
        )
