"""LLM Proxy routes - OpenAI API proxy with auth, logging, and cost tracking.

Endpoints:
- POST /v1/responses - OpenAI Responses API proxy (non-streaming only)

Features:
- Validates agent auth tokens against Postgres
- Enforces model restrictions per agent run
- Logs all requests/responses to llm_requests table
- Tracks token usage for cost budgeting
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from props.backend.auth import AuthContext, get_auth_context
from props.db.models import AgentRun, AgentRunStatus, LLMRequest
from props.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter()

# Environment configuration
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_UPSTREAM_URL", "https://api.openai.com")

# Request timeout for upstream OpenAI calls
UPSTREAM_TIMEOUT_SECONDS = 300  # 5 minutes


def _get_llm_auth_context(request: Request) -> tuple[UUID, str]:
    """Get and validate auth context for LLM requests.

    Returns (agent_run_id, allowed_model) or raises HTTPException.
    """
    auth: AuthContext = get_auth_context(request)

    # Check for auth errors
    if auth.error:
        raise HTTPException(status_code=401, detail=auth.error)

    # Require authentication
    if not auth.is_authenticated:
        raise HTTPException(status_code=401, detail="Authorization required")

    # Require agent credentials (not admin)
    if auth.agent_run_id is None:
        raise HTTPException(status_code=401, detail="Invalid agent token format")

    # Look up agent run to get allowed model and verify status
    with get_session() as session:
        agent_run = session.get(AgentRun, auth.agent_run_id)
        if agent_run is None:
            raise HTTPException(status_code=401, detail="Agent run not found")

        if agent_run.status != AgentRunStatus.IN_PROGRESS:
            raise HTTPException(status_code=403, detail=f"Agent run is not in progress (status={agent_run.status})")

        return auth.agent_run_id, agent_run.model


def _log_request(
    session: Session,
    agent_run_id: UUID,
    model: str,
    request_body: dict[str, Any],
    response_body: dict[str, Any] | None,
    error: str | None,
    latency_ms: int,
) -> None:
    """Log LLM request to database."""
    llm_request = LLMRequest(
        agent_run_id=agent_run_id,
        model=model,
        request_body=request_body,
        response_body=response_body,
        error=error,
        latency_ms=latency_ms,
    )
    session.add(llm_request)
    session.commit()


@router.post("/v1/responses")
async def responses(request: Request) -> JSONResponse:
    """Proxy OpenAI Responses API requests.

    Validates model against agent's allowed model, forwards to OpenAI,
    logs request/response, and returns the response.
    """
    # Get auth context and validate
    agent_run_id, allowed_model = _get_llm_auth_context(request)

    # Parse request body
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e}")

    request_model = body.get("model")
    if not request_model:
        raise HTTPException(status_code=400, detail="model field is required")

    # Enforce model restriction
    if request_model != allowed_model:
        raise HTTPException(
            status_code=403, detail=f"Model '{request_model}' not allowed. Agent is restricted to '{allowed_model}'"
        )

    # Reject streaming requests
    if body.get("stream"):
        raise HTTPException(status_code=400, detail="Streaming is not supported")

    # Reject stateful API modes (we log everything ourselves)
    if body.get("store"):
        raise HTTPException(status_code=400, detail="Stateful mode 'store' is not supported")
    if body.get("previous_response_id"):
        raise HTTPException(status_code=400, detail="Stateful mode 'previous_response_id' is not supported")

    # Forward request to OpenAI
    start_time = time.monotonic()
    upstream_url = f"{OPENAI_BASE_URL}/v1/responses"

    async with httpx.AsyncClient() as client:
        try:
            upstream_response = await client.post(
                upstream_url,
                json=body,
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                timeout=UPSTREAM_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException:
            latency_ms = int((time.monotonic() - start_time) * 1000)
            with get_session() as session:
                _log_request(
                    session=session,
                    agent_run_id=agent_run_id,
                    model=request_model,
                    request_body=body,
                    response_body=None,
                    error="Upstream timeout",
                    latency_ms=latency_ms,
                )
            raise HTTPException(status_code=504, detail="Upstream timeout")
        except httpx.RequestError as e:
            latency_ms = int((time.monotonic() - start_time) * 1000)
            with get_session() as session:
                _log_request(
                    session=session,
                    agent_run_id=agent_run_id,
                    model=request_model,
                    request_body=body,
                    response_body=None,
                    error=str(e),
                    latency_ms=latency_ms,
                )
            raise HTTPException(status_code=502, detail=f"Upstream error: {e}")

    latency_ms = int((time.monotonic() - start_time) * 1000)

    # Parse response
    try:
        response_body = upstream_response.json()
    except Exception:
        response_body = None

    # Log the request/response
    error = None
    if upstream_response.status_code >= 400:
        error = f"HTTP {upstream_response.status_code}"

    with get_session() as session:
        _log_request(
            session=session,
            agent_run_id=agent_run_id,
            model=request_model,
            request_body=body,
            response_body=response_body,
            error=error,
            latency_ms=latency_ms,
        )

    return JSONResponse(content=response_body, status_code=upstream_response.status_code)
