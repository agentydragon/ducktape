"""REST API client for evaluation endpoints.

Provides a client for PO/PI agent containers to call the backend's eval API.
Replaces the MCP-based PromptEvalServer with direct HTTP calls.

Usage (inside container):
    from props.core.eval_client import EvalClient

    async with EvalClient.from_env() as client:
        result = await client.run_critic(
            definition_id="critic",
            example={"kind": "whole_snapshot", "snapshot_slug": "repo/2025-01-01"},
        )
        status = await client.wait_until_graded(result.critic_run_id)
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

from props.core.eval_api_models import GradingStatusResponse, RunCriticRequest, RunCriticResponse
from props.core.ids import DefinitionId
from props.core.models.examples import ExampleSpec

logger = logging.getLogger(__name__)


# =============================================================================
# Client
# =============================================================================


@dataclass
class EvalClient:
    """REST API client for evaluation endpoints.

    Connects to the props backend to run critic evaluations and check grading status.
    Used by PO/PI agents inside containers as a replacement for MCP.
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

    async def get_grading_status(self, critic_run_id: UUID) -> GradingStatusResponse:
        """Check grading status for a critic run (non-blocking).

        Args:
            critic_run_id: agent_run_id of the critic run

        Returns:
            GradingStatusResponse with completion status and metrics

        Raises:
            httpx.HTTPStatusError: On API errors (4xx, 5xx)
        """
        assert self._client is not None, "Client not initialized - use async with"

        response = await self._client.get(f"/api/eval/grading_status/{critic_run_id}")
        response.raise_for_status()
        return GradingStatusResponse.model_validate(response.json())

    async def wait_until_graded(
        self, critic_run_id: UUID, *, timeout_seconds: int = 300, poll_interval_seconds: int = 5
    ) -> GradingStatusResponse:
        """Wait for a critic run to be fully graded.

        Polls the grading_status endpoint until is_complete=True or timeout.

        Args:
            critic_run_id: agent_run_id of the critic run
            timeout_seconds: Max seconds to wait (default: 300)
            poll_interval_seconds: Polling interval (default: 5)

        Returns:
            GradingStatusResponse with completion status and metrics

        Raises:
            TimeoutError: If grading doesn't complete within timeout
            httpx.HTTPStatusError: On API errors
        """
        start_time = time.monotonic()
        deadline = start_time + timeout_seconds
        last_pending_count: int | None = None

        while time.monotonic() < deadline:
            status = await self.get_grading_status(critic_run_id)

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
