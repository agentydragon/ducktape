"""REST API client for critic run endpoints.

Used by critic developer agents inside containers to call backend's
POST /api/runs/critic endpoint.

Usage (inside container):
    from props.agents.critic_dev.eval_client import CriticRunClient

    async with CriticRunClient.from_env() as client:
        result = await client.start_critic(
            RunCriticRequest(
                definition_id="latest",
                example=WholeSnapshotExample(snapshot_slug="repo/2025-01-01"),
                timeout_seconds=300,
                budget_usd=1.0,
                critic_model="gpt-4o",
            )
        )
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Self

import httpx

from props.core.eval_api_models import RunCriticRequest, StartCriticResponse

logger = logging.getLogger(__name__)


@dataclass
class CriticRunClient:
    """REST API client for critic run endpoints.

    Connects to the props backend to start critic evaluations.
    Used by critic developer agents inside containers.

    For waiting until the critic exits, use wait_until_critic_completed() in loop.py
    which polls the database directly.
    For waiting until graded, use props.agents.critic_dev.grading.wait_until_graded()
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
        self._client = httpx.AsyncClient(
            base_url=self.backend_url, auth=self.auth, timeout=httpx.Timeout(60.0, connect=30.0)
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def start_critic(self, request: RunCriticRequest) -> StartCriticResponse:
        """Start a critic agent. Returns immediately with critic_run_id.

        Raises:
            httpx.HTTPStatusError: On API errors (4xx, 5xx)
        """
        assert self._client is not None, "Client not initialized - use async with"
        response = await self._client.post("/api/runs/critic", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return StartCriticResponse.model_validate(response.json())
