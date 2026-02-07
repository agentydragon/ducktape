"""LLM Proxy routes - OpenAI API proxy with auth, logging, and cost tracking.

TODO: Rename this file to openai_responses_api.py since that's what we're emulating.

Endpoints:
- POST /v1/responses - OpenAI Responses API proxy (non-streaming only)

Features:
- Validates agent auth tokens against Postgres
- Enforces model restrictions per agent run
- Enforces budget limits (rejects requests when budget exceeded)
- Logs all requests/responses to llm_requests table
- Extracts token usage from responses for cost tracking
"""

from __future__ import annotations

import logging
import os
import time
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from props.backend.auth import Auth
from props.backend.deps import AdminDb
from props.db.models import AgentRun, AgentRunStatus, LLMRequest

logger = logging.getLogger(__name__)

router = APIRouter()

# Request timeout for upstream OpenAI calls
UPSTREAM_TIMEOUT_SECONDS = 300  # 5 minutes


def _upstream_api_key() -> str:
    """Read upstream OpenAI API key at call time (not import time) for testability."""
    return os.environ.get("OPENAI_API_KEY", "")


def _upstream_base_url() -> str:
    """Read upstream OpenAI base URL at call time (not import time) for testability."""
    return os.environ.get("OPENAI_UPSTREAM_URL", "https://api.openai.com")


def _check_budget(session: Session, agent_run_id: UUID, budget_usd: float) -> None:
    """Check if agent has exceeded its budget. Raises HTTPException(429) if over budget.

    Sums cost_usd from llm_request_costs view for this agent run (and all descendant
    runs via recursive CTE on parent_agent_run_id).
    """
    result = session.execute(
        text("""
            WITH RECURSIVE run_tree AS (
                SELECT agent_run_id FROM agent_runs WHERE agent_run_id = :run_id
                UNION ALL
                SELECT ar.agent_run_id FROM agent_runs ar
                JOIN run_tree rt ON ar.parent_agent_run_id = rt.agent_run_id
            )
            SELECT COALESCE(SUM(c.cost_usd), 0) AS total_cost
            FROM llm_request_costs c
            JOIN run_tree rt ON c.agent_run_id = rt.agent_run_id
        """),
        {"run_id": agent_run_id},
    )
    total_cost = result.scalar_one()
    if total_cost >= budget_usd:
        raise HTTPException(
            status_code=429,
            detail=f"Budget exceeded: spent ${total_cost:.4f} of ${budget_usd:.2f} budget",
        )


def require_llm_access(auth: Auth, admin_db: AdminDb) -> tuple[UUID, str, float]:
    """FastAPI dependency requiring LLM API access (agent credentials only).

    Returns (agent_run_id, allowed_model, budget_usd) or raises HTTPException.
    """
    if not auth.is_authenticated:
        raise HTTPException(status_code=401, detail="Authorization required")

    if auth.agent_run_id is None:
        raise HTTPException(status_code=401, detail="Invalid agent token format")

    with admin_db.session() as session:
        agent_run = session.get(AgentRun, auth.agent_run_id)
        if agent_run is None:
            raise HTTPException(status_code=401, detail="Agent run not found")

        if agent_run.status != AgentRunStatus.IN_PROGRESS:
            raise HTTPException(status_code=403, detail=f"Agent run is not in progress (status={agent_run.status})")

        return auth.agent_run_id, agent_run.model, agent_run.budget_usd


def _extract_token_usage(response_body: dict[str, Any] | None) -> tuple[int | None, int | None, int | None]:
    """Extract token counts from OpenAI Responses API response.

    Returns (input_tokens, cached_input_tokens, output_tokens).
    """
    if not response_body:
        return None, None, None
    usage = response_body.get("usage")
    if not isinstance(usage, dict):
        return None, None, None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    # Cached tokens are nested under input_tokens_details in the Responses API
    input_details = usage.get("input_tokens_details") or {}
    cached_input_tokens = input_details.get("cached_tokens")
    return input_tokens, cached_input_tokens, output_tokens


def _log_request(
    session: Session,
    agent_run_id: UUID,
    model: str,
    request_body: dict[str, Any],
    response_body: dict[str, Any] | None,
    error: str | None,
    latency_ms: int,
) -> None:
    """Log LLM request to database with token usage extracted from response."""
    input_tokens, cached_input_tokens, output_tokens = _extract_token_usage(response_body)
    llm_request = LLMRequest(
        agent_run_id=agent_run_id,
        model=model,
        request_body=request_body,
        response_body=response_body,
        error=error,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
    )
    session.add(llm_request)
    session.commit()


@router.post("/v1/responses")
async def responses(
    request: Request, admin_db: AdminDb, auth: Annotated[tuple[UUID, str, float], Depends(require_llm_access)]
) -> JSONResponse:
    """Proxy OpenAI Responses API requests.

    Validates model against agent's allowed model, checks budget,
    forwards to OpenAI, logs request/response with token usage, and returns the response.
    """
    agent_run_id, allowed_model, budget_usd = auth

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

    # Strip stateful API modes (we log everything ourselves)
    body.pop("store", None)
    if body.get("previous_response_id"):
        raise HTTPException(status_code=400, detail="Stateful mode 'previous_response_id' is not supported")

    # Check budget before forwarding
    with admin_db.session() as session:
        _check_budget(session, agent_run_id, budget_usd)

    # Forward request to OpenAI
    start_time = time.monotonic()
    upstream_url = f"{_upstream_base_url()}/v1/responses"

    async with httpx.AsyncClient() as client:
        try:
            upstream_response = await client.post(
                upstream_url,
                json=body,
                headers={"Authorization": f"Bearer {_upstream_api_key()}", "Content-Type": "application/json"},
                timeout=UPSTREAM_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException:
            latency_ms = int((time.monotonic() - start_time) * 1000)
            with admin_db.session() as session:
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
            with admin_db.session() as session:
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

    with admin_db.session() as session:
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
