"""REST API client for evaluation endpoints.

Provides:
- EvalClient: REST client for PO/PI agents to call backend's /api/eval/run_critic
- wait_until_graded(): Polls grading_pending view until grading is complete

Architecture:
- run_critic: REST API call to backend, which spawns critic container
- wait_until_graded: Direct database polling inside container (no REST call)

Usage (inside container):
    from props.core.eval_client import EvalClient, wait_until_graded

    async with EvalClient.from_env() as client:
        result = await client.run_critic(
            definition_id="critic",
            example={"kind": "whole_snapshot", "snapshot_slug": "repo/2025-01-01"},
        )

    # Wait for grading by polling the database directly (not via REST API)
    grading_result = await wait_until_graded(result.critic_run_id)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Self
from uuid import UUID

import httpx
from sqlalchemy import func

from props.core.agent_helpers import get_current_agent_run_id
from props.core.eval_api_models import GradingStatusResponse, RunCriticRequest, RunCriticResponse
from props.core.ids import DefinitionId
from props.core.models.examples import ExampleSpec
from props.db.examples import Example
from props.db.models import AgentRun, AgentRunStatus, GradingEdge, GradingPending, Snapshot
from props.db.session import get_session

logger = logging.getLogger(__name__)


# =============================================================================
# Database-based grading status check
# =============================================================================


def get_grading_status_from_db(critic_run_id: UUID) -> GradingStatusResponse:
    """Check grading status by querying the database directly.

    Uses the container's RLS-scoped database credentials to query
    the grading_pending view for drift detection.

    Args:
        critic_run_id: agent_run_id of the critic run

    Returns:
        GradingStatusResponse with completion status and metrics
    """
    with get_session() as session:
        # Check for remaining drift using grading_pending view
        pending_count = (
            session.query(func.count())
            .select_from(GradingPending)
            .filter(GradingPending.critique_run_id == critic_run_id)
            .scalar()
            or 0
        )

        if pending_count > 0:
            # Not complete yet - return partial status
            return GradingStatusResponse(is_complete=False, pending_count=pending_count)

        # No drift - critique is fully graded
        critic_run = session.get(AgentRun, critic_run_id)
        if not critic_run:
            raise ValueError(f"Critic run {critic_run_id} not found")

        critic_config = critic_run.critic_config()
        example_spec = critic_config.example
        snapshot_slug = example_spec.snapshot_slug
        snapshot = session.query(Snapshot).filter_by(slug=snapshot_slug).one()
        split = snapshot.split

        # Find matching example to check scope kind
        example = Example.from_spec(session, example_spec)
        scope_kind = example.example_kind

        # Compute grading metrics from edges for this critique
        total_credit = (
            session.query(func.sum(GradingEdge.credit))
            .filter(GradingEdge.critique_run_id == critic_run_id)
            .filter(GradingEdge.tp_id.isnot(None))
            .scalar()
            or 0.0
        )

        max_credit = (
            session.query(GradingEdge.tp_id, GradingEdge.tp_occurrence_id)
            .filter(GradingEdge.critique_run_id == critic_run_id)
            .filter(GradingEdge.tp_id.isnot(None))
            .distinct()
            .count()
        )

        # Find the grader run(s) that contributed edges
        grader_run_ids = (
            session.query(GradingEdge.grader_run_id)
            .filter(GradingEdge.critique_run_id == critic_run_id)
            .distinct()
            .all()
        )
        # Use the first grader run ID for the response (usually there's only one)
        grader_run_id = grader_run_ids[0][0] if grader_run_ids else critic_run_id

        return GradingStatusResponse(
            is_complete=True,
            pending_count=0,
            grader_run_id=grader_run_id,
            total_credit=float(total_credit),
            max_credit=max_credit,
            split=split,
            example_kind=scope_kind,
        )


async def wait_until_graded(
    critic_run_id: UUID, *, timeout_seconds: int = 300, poll_interval_seconds: int = 5
) -> GradingStatusResponse:
    """Wait for a critic run to be fully graded by polling the database.

    Polls the grading_pending view directly using the container's database
    credentials until there's no more drift (all edges graded) or timeout.

    Validates that:
    - The critic run exists and is not IN_PROGRESS (must be in a terminal state)
    - The critic run was started by the current agent (parent_agent_run_id check)

    Args:
        critic_run_id: agent_run_id of the critic run
        timeout_seconds: Max seconds to wait (default: 300)
        poll_interval_seconds: Polling interval (default: 5)

    Returns:
        GradingStatusResponse with completion status and metrics

    Raises:
        ValueError: If critic run doesn't exist, isn't finished, or wasn't started by this agent
        TimeoutError: If grading doesn't complete within timeout
    """
    # Validate the critic run before waiting
    with get_session() as session:
        critic_run = session.get(AgentRun, critic_run_id)
        if critic_run is None:
            raise ValueError(f"Critic run {critic_run_id} not found")

        # Check that critic run is in a terminal state (not IN_PROGRESS)
        if critic_run.status == AgentRunStatus.IN_PROGRESS:
            raise ValueError(
                f"Critic run {critic_run_id} is still in progress (status: {critic_run.status}). "
                f"wait_until_graded only works on finished runs."
            )

        # Verify this critic run was started by the current agent
        current_agent_id = get_current_agent_run_id(session)
        if critic_run.parent_agent_run_id != current_agent_id:
            raise ValueError(
                f"Critic run {critic_run_id} was not started by this agent. "
                f"Expected parent {current_agent_id}, got {critic_run.parent_agent_run_id}."
            )

    start_time = time.monotonic()
    deadline = start_time + timeout_seconds
    last_pending_count: int | None = None

    while time.monotonic() < deadline:
        status = get_grading_status_from_db(critic_run_id)

        if status.is_complete:
            return status

        # Log progress if pending count changed
        if last_pending_count != status.pending_count:
            logger.debug(f"Waiting for grading: {status.pending_count} edges pending")
            last_pending_count = status.pending_count

        await asyncio.sleep(poll_interval_seconds)

    raise TimeoutError(
        f"Timeout waiting for critic run {critic_run_id} to be graded. "
        f"Waited {timeout_seconds} seconds, {last_pending_count} edges still pending."
    )


# =============================================================================
# REST API Client
# =============================================================================


@dataclass
class EvalClient:
    """REST API client for evaluation endpoints.

    Connects to the props backend to run critic evaluations.
    Used by PO/PI agents inside containers as a replacement for MCP.

    For waiting until graded, use the standalone wait_until_graded() function
    which polls the database directly instead of the API.
    """

    backend_url: str
    auth: tuple[str, str]  # (username, password) for Basic auth
    _client: httpx.AsyncClient | None = None

    @classmethod
    def from_env(cls) -> Self:
        """Create client from environment variables.

        Uses:
        - PROPS_BACKEND_URL: Backend URL (default: http://props-backend:8000)
        - PGUSER: PostgreSQL username for auth
        - PGPASSWORD: PostgreSQL password for auth
        """
        backend_url = os.environ.get("PROPS_BACKEND_URL", "http://props-backend:8000")
        username = os.environ["PGUSER"]
        password = os.environ["PGPASSWORD"]
        return cls(backend_url=backend_url, auth=(username, password))

    async def __aenter__(self) -> Self:
        """Enter async context - create HTTP client."""
        self._client = httpx.AsyncClient(
            base_url=self.backend_url,
            auth=self.auth,
            timeout=httpx.Timeout(3600.0, connect=30.0),  # Long timeout for critic runs
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit async context - close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def run_critic(
        self,
        *,
        definition_id: DefinitionId,
        example: ExampleSpec,
        timeout_seconds: int = 3600,
        budget_usd: float | None = None,
        critic_model: str = "gpt-5.1-codex-mini",
    ) -> RunCriticResponse:
        """Run a critic agent on an example.

        Args:
            definition_id: Agent package ID (e.g., 'critic' or a digest)
            example: Example to evaluate
            timeout_seconds: Max seconds before container is killed
            budget_usd: Max USD cost for this agent
            critic_model: Model for the critic agent

        Returns:
            RunCriticResponse with critic_run_id and status

        Raises:
            httpx.HTTPStatusError: On API errors (4xx, 5xx)
        """
        assert self._client is not None, "Client not initialized - use async with"

        request = RunCriticRequest(
            definition_id=definition_id,
            example=example,
            timeout_seconds=timeout_seconds,
            budget_usd=budget_usd,
            critic_model=critic_model,
        )
        response = await self._client.post("/api/eval/run_critic", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return RunCriticResponse.model_validate(response.json())
