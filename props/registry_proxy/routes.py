"""OCI Registry Proxy routes - ACL enforcement and metadata tracking.

Endpoints:
- GET, HEAD /v2/ - API version check (returns 401 auth challenge if unauthenticated)
- PUT /v2/{repo}/manifests/{ref} - Push manifest with metadata recording
- All other /v2/* - Proxied with method-based ACL (GET/HEAD=read, POST/PATCH/PUT=write)
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from starlette.background import BackgroundTask

from props.backend.auth import (
    AgentRole,
    AnonymousIdentity,
    Auth,
    AuthenticatedIdentity,
    RequestIdentity,
    is_admin_or_evaluator,
    is_critic_dev_agent,
)
from props.backend.deps import AdminDb
from props.core.oci_utils import BUILTIN_TAG, UpstreamRegistryConfig, get_upstream_registry_config, is_digest
from props.db.database import Database
from props.db.models import AgentDefinition, AgentType
from props.db.notifications import GRADER_DEFINITION_CHANGED_CHANNEL, GraderDefinitionChangedNotification

logger = logging.getLogger(__name__)

router = APIRouter()

_OCI_VERSION_HEADER = {"Docker-Distribution-API-Version": "registry/2.0"}


def _get_upstream() -> UpstreamRegistryConfig:
    """Read upstream registry config from env on each call.

    Avoids caching at import time, which breaks tests that set env vars
    via monkeypatch after module import.
    """
    return get_upstream_registry_config()


async def _proxy_to_upstream(request: Request) -> Response:
    """Forward request to upstream registry and return response."""
    upstream = _get_upstream()
    upstream_url = f"{upstream.url}{upstream.rewrite_path(request.url.path)}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    client = httpx.AsyncClient()
    # Preserve multi-valued headers (e.g. multiple Accept lines from Docker)
    # by using a list of tuples instead of dict (which deduplicates keys).
    # Strip the client's Authorization header — upstream uses its own credentials.
    headers = [(k, v) for k, v in request.headers.raw if k not in (b"host", b"authorization")]
    auth = upstream.auth_header()
    if auth:
        headers.append((b"authorization", auth.encode()))

    body = await request.body() if request.method not in ("GET", "HEAD") else b""

    try:
        # 600s: agent image layers are large and can take minutes to stream
        # through to the upstream; the old 30s read timeout surfaced as
        # `502 Upstream error` on big blobs.
        upstream_request = client.build_request(
            method=request.method, url=upstream_url, headers=headers, content=body, timeout=600.0
        )
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.RequestError as e:
        await client.aclose()
        logger.exception("Upstream request failed")
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}")

    # Rewrite Location back to the client namespace. The upstream returns
    # blob-upload session URLs under /v2/{project}/<repo>/...; passed through
    # verbatim, the client would push blobs to the project-prefixed path and
    # the manifest couldn't find them (BLOB_UNKNOWN). dict(httpx.Headers)
    # lowercases keys.
    response_headers = dict(upstream_response.headers)
    for header_name in ("location", "content-location"):
        if header_name in response_headers:
            response_headers[header_name] = upstream.rewrite_location(response_headers[header_name])

    async def close_upstream() -> None:
        await upstream_response.aclose()
        await client.aclose()

    return StreamingResponse(
        upstream_response.aiter_bytes(),
        status_code=upstream_response.status_code,
        headers=response_headers,
        background=BackgroundTask(close_upstream),
    )


@dataclass
class _ImageMetadata:
    base_digest: str | None
    display_name: str | None


async def _extract_image_metadata(manifest_body: bytes, repository: str) -> _ImageMetadata:
    """Extract base digest and display name from OCI manifest config labels."""
    try:
        manifest = json.loads(manifest_body)
        config_descriptor = manifest.get("config")
        if not config_descriptor:
            return _ImageMetadata(base_digest=None, display_name=None)

        config_digest = config_descriptor.get("digest")
        if not config_digest:
            return _ImageMetadata(base_digest=None, display_name=None)

        upstream = _get_upstream()
        config_url = f"{upstream.url}/v2/{upstream.repo_path(repository)}/blobs/{config_digest}"
        async with httpx.AsyncClient() as client:
            try:
                auth = upstream.auth_header()
                headers = {"Authorization": auth} if auth else {}
                response = await client.get(config_url, headers=headers, timeout=5.0)
                if response.status_code != 200:
                    return _ImageMetadata(base_digest=None, display_name=None)

                config = response.json()
                labels = config.get("config", {}).get("Labels", {})
                base_digest: str | None = labels.get("org.opencontainers.image.base.digest")
                display_name: str | None = labels.get("org.opencontainers.image.title")

                if base_digest:
                    logger.info(f"Extracted base_digest from annotation: {base_digest}")
                if display_name:
                    logger.info(f"Extracted display_name from annotation: {display_name}")
                return _ImageMetadata(base_digest=base_digest, display_name=display_name)

            except (httpx.RequestError, json.JSONDecodeError) as e:
                logger.warning(f"Error fetching/parsing config blob: {e}")
                return _ImageMetadata(base_digest=None, display_name=None)

    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Error parsing manifest for image metadata extraction: {e}")
        return _ImageMetadata(base_digest=None, display_name=None)


async def _record_manifest_push(
    repository: str, digest: str, manifest_body: bytes, agent_run_id: UUID | None, db: Database
) -> None:
    """Record a manifest push to agent_definitions table."""
    try:
        agent_type = AgentType(repository)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown repository name: {repository}. Must be a valid agent type: {[t.value for t in AgentType]}",
        )

    with db.session() as session:
        existing = session.get(AgentDefinition, digest)
        if existing:
            logger.info(f"Agent definition {digest} already exists, skipping")
            return

        metadata = await _extract_image_metadata(manifest_body, repository)
        definition = AgentDefinition(
            digest=digest,
            agent_type=agent_type,
            created_by_agent_run_id=agent_run_id,
            base_digest=metadata.base_digest,
            display_name=metadata.display_name,
        )
        session.add(definition)
        session.commit()

        logger.info(
            f"Recorded agent definition: {repository}@{digest} "
            f"(type={agent_type}, created_by={agent_run_id}, "
            f"base={metadata.base_digest or 'none'}, display_name={metadata.display_name or 'none'})"
        )


# Routes — order matters: specific routes must precede the catch-all proxy.
# All registry routes are excluded from OpenAPI schema (include_in_schema=False)
# because they're OCI distribution spec endpoints proxied to an upstream registry,
# not part of our API. The catch-all api_route also causes duplicate operationId
# collisions in the generated schema since one function serves multiple methods.


@router.get("/v2/", include_in_schema=False)
@router.head("/v2/", include_in_schema=False)
async def v2_check(auth: Auth) -> Response:
    """OCI API version check with auth challenge.

    Per OCI distribution spec, returns 401 with WWW-Authenticate for
    unauthenticated callers so Docker/crane know to send credentials.
    """
    if isinstance(auth, AnonymousIdentity):
        return Response(status_code=401, headers={**_OCI_VERSION_HEADER, "WWW-Authenticate": 'Basic realm="props"'})
    return Response(content=b"{}", status_code=200, headers=_OCI_VERSION_HEADER)


def _deny(identity: RequestIdentity, action: str) -> HTTPException:
    """Return 401 for anonymous callers (triggers auth challenge), 403 for authenticated."""
    if isinstance(identity, AnonymousIdentity):
        return HTTPException(status_code=401, headers={"WWW-Authenticate": 'Basic realm="props"'})
    return HTTPException(status_code=403, detail=f"{identity} not allowed to {action}")


def _is_grader_builtin_push(repo: str, ref: str) -> bool:
    """True iff this push moves the grader's builtin tag — the only push the
    GraderSupervisor reacts to.

    The supervisor resolves graders by `grader:BUILTIN_TAG`, so only a move of
    that tag changes which image graders run. By-digest pushes and pinned
    per-commit sha tags (e.g. `grader:<gitsha>`, pushed alongside `latest` by
    //props/agents:push_images) do not — notifying on them needlessly restarts
    every grader, cancelling in-flight grades.
    """
    return repo == str(AgentType.GRADER) and ref == BUILTIN_TAG


@router.put("/v2/{repo}/manifests/{ref}", include_in_schema=False)
async def put_manifest(request: Request, repo: str, ref: str, admin_db: AdminDb, auth: Auth) -> Response:
    """Push a manifest — records agent definition on success."""
    agent_run_id = (
        auth.role.agent_run_id if isinstance(auth, AuthenticatedIdentity) and isinstance(auth.role, AgentRole) else None
    )
    if not (is_admin_or_evaluator(auth) or is_critic_dev_agent(auth)):
        raise _deny(auth, "push to registry")

    if not is_digest(ref) and not is_admin_or_evaluator(auth):
        raise _deny(auth, "push by tag")

    body = await request.body()
    response = await _proxy_to_upstream(request)

    if response.status_code in (200, 201):
        manifest_digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
        await _record_manifest_push(repo, manifest_digest, body, agent_run_id, admin_db)

        # When the grader's builtin tag moves, notify the GraderSupervisor so it
        # can (re)start graders that failed due to missing image at startup.
        if _is_grader_builtin_push(repo, ref):
            notification = GraderDefinitionChangedNotification(digest=manifest_digest, tag=ref)
            with admin_db.session() as session:
                session.execute(
                    text("SELECT pg_notify(:channel, :payload)"),
                    {"channel": GRADER_DEFINITION_CHANGED_CHANNEL, "payload": notification.model_dump_json()},
                )
                session.commit()
            logger.info(f"Notified grader definition changed: {repo}:{ref} -> {manifest_digest}")

    return response


@router.api_route("/v2/{path:path}", methods=["GET", "HEAD", "POST", "PATCH", "PUT"], include_in_schema=False)
async def registry_proxy(request: Request, path: str, auth: Auth, admin_db: AdminDb) -> Response:
    """Proxy OCI registry requests with method-based ACL (read for GET/HEAD, write for mutations)."""
    if request.method in ("GET", "HEAD"):
        if not (is_admin_or_evaluator(auth) or is_critic_dev_agent(auth)):
            raise _deny(auth, "read from registry")
    elif not (is_admin_or_evaluator(auth) or is_critic_dev_agent(auth)):
        raise _deny(auth, "push to registry")
    return await _proxy_to_upstream(request)
