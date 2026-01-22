"""Combined Props Proxy - LLM and Registry proxy in a single FastAPI app.

This unified proxy combines:
- LLM Proxy: OpenAI API proxy with auth, logging, and cost tracking (POST /v1/responses)
- Registry Proxy: OCI registry proxy with ACL enforcement (/v2/* endpoints)

Both proxies share:
- Auth middleware for Postgres credentials validation
- Common database dependencies

Architecture:
- Each proxy is a self-contained FastAPI app that can run standalone
- This module mounts both under a single app with shared middleware
- Routes don't conflict: /v1/* for LLM, /v2/* for Registry

Endpoints:
- GET /health - Combined health check
- POST /v1/responses - LLM proxy (OpenAI Responses API)
- /v2/* - Registry proxy (OCI Distribution API)
"""

from __future__ import annotations

from fastapi import FastAPI

from props.proxy.auth import AuthMiddleware

# Import route handlers from sub-apps
from props.proxy.llm_app import responses as llm_responses
from props.proxy.registry_app import (
    complete_blob_upload,
    continue_blob_upload,
    get_blob,
    get_catalog,
    get_manifest,
    get_tags,
    put_manifest,
    start_blob_upload,
    v2_check,
)

# Create combined FastAPI app
app = FastAPI(title="Props Proxy", description="Combined LLM and Registry proxy for Props agent infrastructure")

# Add shared auth middleware
app.add_middleware(AuthMiddleware)


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


# --- LLM Proxy routes ---
app.post("/v1/responses")(llm_responses)

# --- Registry Proxy routes (OCI Distribution API) ---
app.get("/v2/")(v2_check)
app.head("/v2/")(v2_check)
app.get("/v2/_catalog")(get_catalog)
app.get("/v2/{repo}/tags/list")(get_tags)
app.get("/v2/{repo}/manifests/{ref}")(get_manifest)
app.head("/v2/{repo}/manifests/{ref}")(get_manifest)
app.put("/v2/{repo}/manifests/{ref}")(put_manifest)
app.get("/v2/{repo}/blobs/{digest}")(get_blob)
app.head("/v2/{repo}/blobs/{digest}")(get_blob)
app.post("/v2/{repo}/blobs/uploads/")(start_blob_upload)
app.patch("/v2/{repo}/blobs/uploads/{uuid}")(continue_blob_upload)
app.put("/v2/{repo}/blobs/uploads/{uuid}")(complete_blob_upload)
