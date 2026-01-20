"""LLM Proxy - OpenAI API proxy with auth, logging, and cost tracking.

Sits between agent containers and the OpenAI API to:
- Validate agent auth tokens against Postgres
- Enforce model restrictions per agent run
- Log all requests/responses to llm_requests table
- Track token usage for cost budgeting

The proxy implements a subset of the OpenAI Responses API:
- POST /v1/responses (non-streaming only)

Streaming is not supported to simplify logging and cost tracking.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from props.core.db.models import AgentRun, AgentRunStatus, LLMRequest
from props.core.db.session import get_session

logger = logging.getLogger(__name__)

# Environment configuration
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_UPSTREAM_URL", "https://api.openai.com")
PGHOST = os.environ.get("PGHOST", "props-postgres")
PGPORT = os.environ.get("PGPORT", "5432")
PGDATABASE = os.environ.get("PGDATABASE", "eval_results")

# Request timeout for upstream OpenAI calls
UPSTREAM_TIMEOUT_SECONDS = 300  # 5 minutes


@dataclass
class AuthContext:
    """Authenticated agent context."""

    agent_run_id: UUID
    allowed_model: str


def _validate_postgres_credentials(username: str, password: str) -> bool:
    """Validate credentials by attempting Postgres connection."""
    try:
        with psycopg.connect(
            host=PGHOST, port=PGPORT, dbname=PGDATABASE, user=username, password=password, connect_timeout=5
        ):
            return True
    except psycopg.OperationalError:
        return False


def _parse_auth_header(authorization: str | None) -> tuple[str, str]:
    """Parse Basic auth header, raise HTTPException on failure."""
    if not authorization or not authorization.startswith("Basic "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization required")

    try:
        encoded = authorization.removeprefix("Basic ")
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, password = decoded.split(":", 1)
        return (username, password)
    except (ValueError, UnicodeDecodeError) as e:
        logger.warning(f"Failed to parse Basic auth: {e}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authorization format")


def get_auth(authorization: Annotated[str | None, Header()] = None) -> AuthContext:
    """Dependency to extract and validate agent auth.

    Validates:
    1. Basic auth credentials work against Postgres
    2. Username matches agent_{uuid} pattern
    3. Agent run exists and is in progress
    4. Returns the allowed model from the agent run
    """
    username, password = _parse_auth_header(authorization)

    # Validate credentials against Postgres
    if not _validate_postgres_credentials(username, password):
        logger.warning(f"Invalid postgres credentials for user: {username}")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Extract agent_run_id from username pattern: agent_{uuid}
    if not username.startswith("agent_"):
        raise HTTPException(status_code=401, detail="Invalid agent token format")

    try:
        agent_run_id = UUID(username.removeprefix("agent_"))
    except ValueError:
        logger.warning(f"Invalid UUID in agent username: {username}")
        raise HTTPException(status_code=401, detail="Invalid agent token")

    # Look up agent run to get allowed model and verify status
    with get_session() as session:
        agent_run = session.get(AgentRun, agent_run_id)
        if agent_run is None:
            raise HTTPException(status_code=401, detail="Agent run not found")

        if agent_run.status != AgentRunStatus.IN_PROGRESS:
            raise HTTPException(
                status_code=403, detail=f"Agent run is not in progress (status={agent_run.status})"
            )

        # The allowed model is stored in agent_run.model
        allowed_model = agent_run.model

    return AuthContext(agent_run_id=agent_run_id, allowed_model=allowed_model)


class ResponsesRequest(BaseModel):
    """OpenAI Responses API request body."""

    model: str
    input: list[dict[str, Any]]
    instructions: str | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    stream: Literal[False] = False


def _log_request(
    session: Session,
    agent_run_id: UUID,
    model: str,
    request_body: dict[str, Any],
    response_body: dict[str, Any] | None,
    error: str | None,
    latency_ms: int,
) -> None:
    """Log LLM request to database.

    Token counts are computed via llm_request_costs view from response_body.
    """
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


# FastAPI app
app = FastAPI(title="Props LLM Proxy")


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/v1/responses")
async def responses(
    request: Request, auth: Annotated[AuthContext, Depends(get_auth)]
) -> JSONResponse:
    """Proxy OpenAI Responses API requests.

    Validates model against agent's allowed model, forwards to OpenAI,
    logs request/response, and returns the response.
    """
    # Parse request body
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e}")

    request_model = body.get("model")
    if not request_model:
        raise HTTPException(status_code=400, detail="model field is required")

    # Enforce model restriction
    if request_model != auth.allowed_model:
        raise HTTPException(
            status_code=403,
            detail=f"Model '{request_model}' not allowed. Agent is restricted to '{auth.allowed_model}'",
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
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=UPSTREAM_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException:
            latency_ms = int((time.monotonic() - start_time) * 1000)
            # Log the timeout
            with get_session() as session:
                _log_request(
                    session=session,
                    agent_run_id=auth.agent_run_id,
                    model=request_model,
                    request_body=body,
                    response_body=None,
                    error="Upstream timeout",
                    latency_ms=latency_ms,
                )
            raise HTTPException(status_code=504, detail="Upstream timeout")
        except httpx.RequestError as e:
            latency_ms = int((time.monotonic() - start_time) * 1000)
            # Log the error
            with get_session() as session:
                _log_request(
                    session=session,
                    agent_run_id=auth.agent_run_id,
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
            agent_run_id=auth.agent_run_id,
            model=request_model,
            request_body=body,
            response_body=response_body,
            error=error,
            latency_ms=latency_ms,
        )

    # Return response with same status code
    return JSONResponse(content=response_body, status_code=upstream_response.status_code)
