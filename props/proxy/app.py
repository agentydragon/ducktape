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

# Import route handlers from sub-apps (we'll add them as routes, not mount)
from props.proxy.llm_app import responses as llm_responses
from props.proxy.registry_app import proxy as registry_proxy

# Create combined FastAPI app
app = FastAPI(title="Props Proxy", description="Combined LLM and Registry proxy for Props agent infrastructure")

# Add shared auth middleware
app.add_middleware(AuthMiddleware)


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


# Add LLM route
app.post("/v1/responses")(llm_responses)

# Add Registry catch-all route
app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])(registry_proxy)
