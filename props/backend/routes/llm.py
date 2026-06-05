"""LLM Proxy routes - OpenAI API proxy with auth, logging, and cost tracking.

Endpoints:
- POST /v1/responses - OpenAI Responses API proxy (non-streaming only)
- POST /v1/chat/completions - OpenAI Chat Completions API proxy (non-streaming only)

Features:
- Validates agent auth tokens against Postgres
- Enforces model restrictions per agent run
- Enforces budget limits (rejects requests when budget exceeded)
- Logs all requests/responses to llm_requests table
- Extracts token usage from responses for cost tracking
- Multi-upstream routing via model_metadata.upstream_name/upstream_model
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from openai_utils.api_shape import LLMApiShape
from openai_utils.model import ResponseUsage
from props.backend.auth import AgentRole, Auth, AuthenticatedIdentity
from props.backend.deps import AdminDb, Config
from props.config import LLMProxyConfig, UpstreamConfig
from props.db.models import AgentRun, AgentRunBudgetStatus, AgentRunStatus, LLMRequest, ModelMetadata

logger = logging.getLogger(__name__)

router = APIRouter()

# Request timeout for upstream OpenAI calls
UPSTREAM_TIMEOUT_SECONDS = 300  # 5 minutes

# Default OpenAI upstream config used when model has no explicit upstream
DEFAULT_OPENAI_UPSTREAM = UpstreamConfig(url_env="OPENAI_BASE_URL", api_key_env="OPENAI_API_KEY")


@dataclass
class UpstreamRoute:
    """Resolved upstream routing info for a model."""

    url: str
    api_key: str
    model_name: str  # Model name to send in API request


@dataclass(frozen=True)
class LLMAccess:
    agent_run_id: UUID
    allowed_model: str
    budget_usd: float
    parent_agent_run_id: UUID | None
    agent_type: str


@dataclass(frozen=True)
class LLMUsageCounts:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None


def _resolve_upstream_url(config: UpstreamConfig) -> str:
    """Resolve URL from static url or url_env."""
    if config.url:
        return config.url
    if config.url_env:
        return os.environ.get(config.url_env, "https://api.openai.com/v1")
    raise ValueError("Upstream config must have url or url_env")


def _get_upstream_route(
    model_id: str, session: Session, config: LLMProxyConfig, api_shape: LLMApiShape
) -> UpstreamRoute:
    """Look up upstream routing info for a model.

    Returns UpstreamRoute with resolved URL, API key, and model name to send.
    """
    metadata = session.get(ModelMetadata, model_id)
    if metadata is None:
        raise HTTPException(status_code=400, detail=f"Unknown model: {model_id}")
    if metadata.api_shape != api_shape.value:
        raise HTTPException(
            status_code=400, detail=f"Model '{model_id}' uses api_shape='{metadata.api_shape}', not '{api_shape.value}'"
        )

    # Determine which upstream to use (NULL = "openai" default)
    upstream_name = metadata.upstream_name
    upstream_config: UpstreamConfig
    if upstream_name is None:
        upstream_config = DEFAULT_OPENAI_UPSTREAM
    else:
        maybe_config = config.upstreams.get(upstream_name)
        if maybe_config is None:
            raise HTTPException(
                status_code=500, detail=f"Model {model_id} references unknown upstream: {upstream_name}"
            )
        upstream_config = maybe_config

    # Resolve URL and API key
    upstream_url = _resolve_upstream_url(upstream_config)
    api_key = os.environ.get(upstream_config.api_key_env, "")

    # Determine model name to send (NULL = use model_id)
    model_name = metadata.upstream_model or model_id

    return UpstreamRoute(url=upstream_url, api_key=api_key, model_name=model_name)


def _check_budget(session: Session, agent_run_id: UUID, budget_usd: float) -> None:
    """Check if agent has exceeded its budget. Raises HTTPException(429) if over budget.

    Uses the agent_run_budget_status view which recursively sums descendant costs.
    Zero-cost calls (e.g. local models with $0 pricing) are always allowed.
    """
    status = session.get(AgentRunBudgetStatus, agent_run_id)
    if status is None:
        raise HTTPException(status_code=500, detail=f"Agent run {agent_run_id} not found in budget view")
    if status.tree_spent_usd == 0 and budget_usd == 0:
        return
    if status.tree_spent_usd >= budget_usd:
        raise HTTPException(
            status_code=429, detail=f"Budget exceeded: spent ${status.tree_spent_usd:.4f} of ${budget_usd:.2f} budget"
        )


def require_llm_access(auth: Auth, admin_db: AdminDb) -> LLMAccess:
    """FastAPI dependency requiring LLM API access (agent credentials only).

    Returns (agent_run_id, allowed_model, budget_usd) or raises HTTPException.
    """
    if not (isinstance(auth, AuthenticatedIdentity) and isinstance(auth.role, AgentRole)):
        raise HTTPException(status_code=401, detail="Authorization required")

    with admin_db.session() as session:
        agent_run = session.get(AgentRun, auth.role.agent_run_id)
        if agent_run is None:
            raise HTTPException(status_code=401, detail="Agent run not found")

        if agent_run.status != AgentRunStatus.IN_PROGRESS:
            raise HTTPException(status_code=403, detail=f"Agent run is not in progress (status={agent_run.status})")

        return LLMAccess(
            agent_run_id=auth.role.agent_run_id,
            allowed_model=agent_run.model,
            budget_usd=agent_run.budget_usd,
            parent_agent_run_id=agent_run.parent_agent_run_id,
            agent_type=agent_run.type_config.agent_type.value,
        )


def _extract_responses_usage(response_body: dict[str, Any] | None) -> LLMUsageCounts:
    if response_body is None:
        return LLMUsageCounts()
    usage_body = response_body.get("usage")
    if not isinstance(usage_body, dict):
        return LLMUsageCounts()
    usage = ResponseUsage.model_validate(usage_body)
    return LLMUsageCounts(
        input_tokens=usage.input_tokens,
        cached_input_tokens=usage.input_tokens_details.cached_tokens if usage.input_tokens_details else None,
        output_tokens=usage.output_tokens,
    )


def _extract_chat_completions_usage(response_body: dict[str, Any] | None) -> LLMUsageCounts:
    if response_body is None:
        return LLMUsageCounts()
    usage_body = response_body.get("usage")
    if not isinstance(usage_body, dict):
        return LLMUsageCounts()
    input_details = usage_body.get("prompt_tokens_details")
    cached_tokens = input_details.get("cached_tokens") if isinstance(input_details, dict) else None
    return LLMUsageCounts(
        input_tokens=usage_body.get("prompt_tokens") if isinstance(usage_body.get("prompt_tokens"), int) else None,
        cached_input_tokens=cached_tokens if isinstance(cached_tokens, int) else None,
        output_tokens=usage_body.get("completion_tokens")
        if isinstance(usage_body.get("completion_tokens"), int)
        else None,
    )


def _extract_usage(api_shape: LLMApiShape, response_body: dict[str, Any] | None) -> LLMUsageCounts:
    if api_shape == LLMApiShape.RESPONSES:
        return _extract_responses_usage(response_body)
    if api_shape == LLMApiShape.CHAT_COMPLETIONS:
        return _extract_chat_completions_usage(response_body)
    raise ValueError(f"Unsupported LLM API shape: {api_shape}")


def _log_request(
    session: Session,
    agent_run_id: UUID,
    model: str,
    api_shape: LLMApiShape,
    request_body: dict[str, Any],
    response_body: dict[str, Any] | None,
    error: str | None,
    latency_ms: int,
) -> None:
    """Log LLM request to database with token usage extracted from response."""
    usage = LLMUsageCounts()
    if response_body is not None and error is None:
        usage = _extract_usage(api_shape, response_body)
    llm_request = LLMRequest(
        agent_run_id=agent_run_id,
        model=model,
        api_shape=api_shape.value,
        request_body=request_body,
        response_body=response_body,
        error=error,
        input_tokens=usage.input_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        output_tokens=usage.output_tokens,
        latency_ms=latency_ms,
    )
    session.add(llm_request)
    session.commit()


def _merge_props_metadata(
    body: dict[str, Any], *, access: LLMAccess, api_shape: LLMApiShape, logical_model: str, upstream_model: str
) -> None:
    metadata = body.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=400, detail="metadata must be an object when provided")

    props_metadata: dict[str, str] = {
        "props.agent_run_id": str(access.agent_run_id),
        "props.agent_type": access.agent_type,
        "props.api_shape": api_shape.value,
        "props.logical_model": logical_model,
        "props.upstream_model": upstream_model,
    }
    if access.parent_agent_run_id is not None:
        props_metadata["props.parent_agent_run_id"] = str(access.parent_agent_run_id)

    body["metadata"] = {**metadata, **props_metadata}


async def _read_json_object(request: Request) -> dict[str, Any]:
    try:
        raw_body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e}")
    if not isinstance(raw_body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return raw_body


async def _proxy_openai_request(
    request: Request,
    admin_db: AdminDb,
    config: Config,
    access: LLMAccess,
    *,
    api_shape: LLMApiShape,
    upstream_path: str,
) -> JSONResponse:
    body = await _read_json_object(request)

    request_model = body.get("model")
    if not isinstance(request_model, str) or not request_model:
        raise HTTPException(status_code=400, detail="model field is required")

    # Enforce model restriction
    if request_model != access.allowed_model:
        raise HTTPException(
            status_code=403,
            detail=f"Model '{request_model}' not allowed. Agent is restricted to '{access.allowed_model}'",
        )

    # Reject streaming requests
    if body.get("stream"):
        raise HTTPException(status_code=400, detail="Streaming is not supported")

    if api_shape == LLMApiShape.RESPONSES:
        # Strip stateful API modes (we log everything ourselves)
        body.pop("store", None)
        if body.get("previous_response_id"):
            raise HTTPException(status_code=400, detail="Stateful mode 'previous_response_id' is not supported")

    # Resolve upstream routing and check budget
    with admin_db.session() as session:
        _check_budget(session, access.agent_run_id, access.budget_usd)
        upstream = _get_upstream_route(request_model, session, config, api_shape)

    # Rewrite model in request body to upstream model name
    body["model"] = upstream.model_name
    _merge_props_metadata(
        body, access=access, api_shape=api_shape, logical_model=request_model, upstream_model=upstream.model_name
    )

    # Forward request to upstream
    start_time = time.monotonic()
    upstream_url = f"{upstream.url}/{upstream_path}"

    async with httpx.AsyncClient() as client:
        try:
            upstream_response = await client.post(
                upstream_url,
                json=body,
                headers={"Authorization": f"Bearer {upstream.api_key}", "Content-Type": "application/json"},
                timeout=UPSTREAM_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException:
            latency_ms = int((time.monotonic() - start_time) * 1000)
            with admin_db.session() as session:
                _log_request(
                    session=session,
                    agent_run_id=access.agent_run_id,
                    model=request_model,
                    api_shape=api_shape,
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
                    agent_run_id=access.agent_run_id,
                    model=request_model,
                    api_shape=api_shape,
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
            agent_run_id=access.agent_run_id,
            model=request_model,
            api_shape=api_shape,
            request_body=body,
            response_body=response_body,
            error=error,
            latency_ms=latency_ms,
        )

    return JSONResponse(content=response_body, status_code=upstream_response.status_code)


@router.post("/v1/responses")
async def responses(
    request: Request, admin_db: AdminDb, config: Config, auth: Annotated[LLMAccess, Depends(require_llm_access)]
) -> JSONResponse:
    """Proxy OpenAI Responses API requests."""
    return await _proxy_openai_request(
        request, admin_db, config, auth, api_shape=LLMApiShape.RESPONSES, upstream_path="responses"
    )


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request, admin_db: AdminDb, config: Config, auth: Annotated[LLMAccess, Depends(require_llm_access)]
) -> JSONResponse:
    """Proxy OpenAI Chat Completions API requests."""
    return await _proxy_openai_request(
        request, admin_db, config, auth, api_shape=LLMApiShape.CHAT_COMPLETIONS, upstream_path="chat/completions"
    )
