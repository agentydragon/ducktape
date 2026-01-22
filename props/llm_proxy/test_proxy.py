"""Tests for LLM proxy service.

Tests cover:
- Authentication (valid/invalid credentials)
- Model enforcement
- Request/response logging to llm_requests table
- RLS for recursive subagent access to llm_requests
"""

from __future__ import annotations

import base64
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import pytest_bazel
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from openai_utils.model import ResponseUsage
from props.db.config import DatabaseConfig
from props.db.examples import Example
from props.db.models import AgentRunStatus, LLMRequest
from props.db.session import get_session
from props.db.temp_user_manager import TempUserCredentials, TempUserManager
from props.llm_proxy.proxy import app
from props.testing.fixtures import make_critic_run

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


def _basic_auth_header(username: str, password: str) -> str:
    """Create Basic auth header value."""
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {credentials}"


@pytest.fixture
def client() -> Generator[TestClient]:
    """Create test client for the LLM proxy."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def train1_example(synced_test_db: DatabaseConfig) -> Example:
    """Provide an Example from the train1 fixture."""
    with get_session() as session:
        example = session.query(Example).filter_by(snapshot_slug="test-fixtures/train1").first()
        assert example, "test-fixtures/train1 fixture not found"
        # Detach from session for use in other transactions
        session.expunge(example)
        return example


@pytest.fixture
def agent_run_id(synced_test_db: DatabaseConfig, train1_example: Example) -> UUID:
    """Create an in-progress agent run and return its ID."""
    run_id = uuid4()
    with get_session() as session:
        agent_run = make_critic_run(
            example=train1_example, model="gpt-4o", status=AgentRunStatus.IN_PROGRESS, agent_run_id=run_id
        )
        session.add(agent_run)
        session.commit()
    return run_id


@pytest_asyncio.fixture
async def agent_creds(synced_test_db: DatabaseConfig, agent_run_id: UUID) -> AsyncGenerator[TempUserCredentials]:
    """Provide temp user credentials for the agent run."""
    async with TempUserManager(synced_test_db.admin, agent_run_id) as creds:
        yield creds


class TestHealthEndpoint:
    """Tests for /health endpoint."""

    def test_health_returns_ok(self, client: TestClient):
        """Health endpoint returns ok status."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestAuthentication:
    """Tests for authentication validation."""

    def test_missing_auth_returns_401(self, client: TestClient):
        """Request without auth header returns 401."""
        response = client.post("/v1/responses", json={"model": "gpt-4o", "input": []})
        assert response.status_code == 401
        assert "Authorization required" in response.json()["detail"]

    def test_invalid_auth_format_returns_401(self, client: TestClient):
        """Request with invalid auth format returns 401."""
        response = client.post(
            "/v1/responses", json={"model": "gpt-4o", "input": []}, headers={"Authorization": "Bearer invalid"}
        )
        assert response.status_code == 401
        assert "Invalid authorization format" in response.json()["detail"]

    def test_invalid_credentials_returns_401(self, client: TestClient, synced_test_db: DatabaseConfig):
        """Request with wrong password returns 401."""
        response = client.post(
            "/v1/responses",
            json={"model": "gpt-4o", "input": []},
            headers={"Authorization": _basic_auth_header("agent_00000000-0000-0000-0000-000000000000", "wrong")},
        )
        assert response.status_code == 401

    def test_non_agent_username_returns_401(self, client: TestClient, synced_test_db: DatabaseConfig):
        """Request with non-agent username pattern returns 401."""
        response = client.post(
            "/v1/responses",
            json={"model": "gpt-4o", "input": []},
            headers={"Authorization": _basic_auth_header("postgres", "password")},
        )
        assert response.status_code == 401
        assert "Invalid agent token format" in response.json()["detail"]


class TestModelEnforcement:
    """Tests for model restriction enforcement."""

    @patch("props.llm_proxy.proxy.httpx.AsyncClient")
    async def test_wrong_model_returns_403(
        self, mock_client_class: AsyncMock, client: TestClient, agent_creds: TempUserCredentials
    ):
        """Request for non-allowed model returns 403."""
        creds = agent_creds

        response = client.post(
            "/v1/responses",
            json={"model": "gpt-3.5-turbo", "input": []},  # Agent is restricted to gpt-4o
            headers={"Authorization": _basic_auth_header(creds.username, creds.password)},
        )
        assert response.status_code == 403
        assert "not allowed" in response.json()["detail"]
        assert "gpt-4o" in response.json()["detail"]

    @patch("props.llm_proxy.proxy.httpx.AsyncClient")
    async def test_correct_model_forwards_request(
        self, mock_client_class: AsyncMock, client: TestClient, agent_creds: TempUserCredentials
    ):
        """Request with allowed model is forwarded to upstream."""
        creds = agent_creds

        # Mock the upstream response
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "resp_123",
            "output": [{"type": "message", "content": [{"type": "text", "text": "Hello"}]}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        response = client.post(
            "/v1/responses",
            json={"model": "gpt-4o", "input": [{"role": "user", "content": "Hi"}]},
            headers={"Authorization": _basic_auth_header(creds.username, creds.password)},
        )

        assert response.status_code == 200
        assert mock_client.post.called


class TestStreamingRejection:
    """Tests for streaming request rejection."""

    def test_streaming_request_returns_400(self, client: TestClient, agent_creds: TempUserCredentials):
        """Request with stream=true returns 400."""
        creds = agent_creds

        response = client.post(
            "/v1/responses",
            json={"model": "gpt-4o", "input": [], "stream": True},
            headers={"Authorization": _basic_auth_header(creds.username, creds.password)},
        )
        assert response.status_code == 400
        assert "Streaming is not supported" in response.json()["detail"]


class TestStatefulModeRejection:
    """Tests for stateful API mode rejection."""

    def test_store_mode_returns_400(self, client: TestClient, agent_creds: TempUserCredentials):
        """Request with store=true returns 400."""
        creds = agent_creds

        response = client.post(
            "/v1/responses",
            json={"model": "gpt-4o", "input": [], "store": True},
            headers={"Authorization": _basic_auth_header(creds.username, creds.password)},
        )
        assert response.status_code == 400
        assert "store" in response.json()["detail"]

    def test_previous_response_id_returns_400(self, client: TestClient, agent_creds: TempUserCredentials):
        """Request with previous_response_id returns 400."""
        creds = agent_creds

        response = client.post(
            "/v1/responses",
            json={"model": "gpt-4o", "input": [], "previous_response_id": "resp_abc123"},
            headers={"Authorization": _basic_auth_header(creds.username, creds.password)},
        )
        assert response.status_code == 400
        assert "previous_response_id" in response.json()["detail"]


class TestRequestLogging:
    """Tests for request/response logging to llm_requests table."""

    @patch("props.llm_proxy.proxy.httpx.AsyncClient")
    async def test_successful_request_is_logged(
        self, mock_client_class: AsyncMock, client: TestClient, agent_run_id: UUID, agent_creds: TempUserCredentials
    ):
        """Successful request is logged to llm_requests table."""
        # Mock upstream response
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "resp_123",
            "output": [],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        response = client.post(
            "/v1/responses",
            json={"model": "gpt-4o", "input": [{"role": "user", "content": "test"}]},
            headers={"Authorization": _basic_auth_header(agent_creds.username, agent_creds.password)},
        )
        assert response.status_code == 200

        # Verify request was logged
        with get_session() as session:
            logged = session.query(LLMRequest).filter_by(agent_run_id=agent_run_id).all()
            assert len(logged) == 1
            assert logged[0].model == "gpt-4o"
            assert logged[0].error is None
            # Token counts stored in response_body, computed via view
            assert logged[0].response_body is not None
            usage = ResponseUsage.model_validate(logged[0].response_body["usage"])
            assert usage.input_tokens == 100
            assert usage.output_tokens == 50


class TestCompletedAgentRejection:
    """Tests that completed agents cannot make requests."""

    async def test_completed_agent_returns_403(
        self, client: TestClient, synced_test_db: DatabaseConfig, train1_example: Example
    ):
        """Completed agent run cannot make LLM requests."""
        run_id = uuid4()

        # Create a COMPLETED agent run
        with get_session() as session:
            agent_run = make_critic_run(
                example=train1_example, model="gpt-4o", status=AgentRunStatus.COMPLETED, agent_run_id=run_id
            )
            session.add(agent_run)
            session.commit()

        async with TempUserManager(synced_test_db.admin, run_id) as creds:
            response = client.post(
                "/v1/responses",
                json={"model": "gpt-4o", "input": []},
                headers={"Authorization": _basic_auth_header(creds.username, creds.password)},
            )
            assert response.status_code == 403
            assert "not in progress" in response.json()["detail"]


class TestRLSRecursiveAccess:
    """Tests for RLS policy allowing recursive subagent access."""

    @patch("props.llm_proxy.proxy.httpx.AsyncClient")
    async def test_parent_can_see_child_llm_requests(
        self, mock_client_class: AsyncMock, client: TestClient, synced_test_db: DatabaseConfig, train1_example: Example
    ):
        """Parent agent can see LLM requests made by child agent."""
        parent_run_id = uuid4()
        child_run_id = uuid4()

        # Create parent and child agent runs
        with get_session() as session:
            # Parent run
            parent_run = make_critic_run(
                example=train1_example, model="gpt-4o", status=AgentRunStatus.IN_PROGRESS, agent_run_id=parent_run_id
            )
            session.add(parent_run)
            session.flush()

            # Child run with parent reference
            child_run = make_critic_run(
                example=train1_example, model="gpt-4o", status=AgentRunStatus.IN_PROGRESS, agent_run_id=child_run_id
            )
            child_run.parent_agent_run_id = parent_run_id
            session.add(child_run)
            session.commit()

        # Mock upstream for child's request
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "resp_child",
            "output": [],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        # Child makes a request (logged as admin via proxy)
        async with TempUserManager(synced_test_db.admin, child_run_id) as child_creds:
            response = client.post(
                "/v1/responses",
                json={"model": "gpt-4o", "input": []},
                headers={"Authorization": _basic_auth_header(child_creds.username, child_creds.password)},
            )
            assert response.status_code == 200

        # Verify child's request was logged
        with get_session() as session:
            child_requests = session.query(LLMRequest).filter_by(agent_run_id=child_run_id).all()
            assert len(child_requests) == 1

        # Parent can see child's requests via RLS
        async with TempUserManager(synced_test_db.admin, parent_run_id) as parent_creds:
            parent_config = synced_test_db.admin.with_user(parent_creds.username, parent_creds.password)
            parent_engine = create_engine(parent_config.url())
            try:
                with Session(parent_engine) as parent_session:
                    # Parent should see child's request
                    visible_requests = parent_session.query(LLMRequest).filter_by(agent_run_id=child_run_id).all()
                    assert len(visible_requests) == 1, "Parent should see child's LLM requests via RLS"
            finally:
                parent_engine.dispose()

    @patch("props.llm_proxy.proxy.httpx.AsyncClient")
    async def test_unrelated_agent_cannot_see_other_requests(
        self, mock_client_class: AsyncMock, client: TestClient, synced_test_db: DatabaseConfig, train1_example: Example
    ):
        """Unrelated agent cannot see another agent's LLM requests."""
        agent1_run_id = uuid4()
        agent2_run_id = uuid4()

        # Create two unrelated agent runs
        with get_session() as session:
            for run_id in [agent1_run_id, agent2_run_id]:
                agent_run = make_critic_run(
                    example=train1_example, model="gpt-4o", status=AgentRunStatus.IN_PROGRESS, agent_run_id=run_id
                )
                session.add(agent_run)
            session.commit()

        # Mock upstream
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "resp",
            "output": [],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        # Agent1 makes a request
        async with TempUserManager(synced_test_db.admin, agent1_run_id) as agent1_creds:
            response = client.post(
                "/v1/responses",
                json={"model": "gpt-4o", "input": []},
                headers={"Authorization": _basic_auth_header(agent1_creds.username, agent1_creds.password)},
            )
            assert response.status_code == 200

        # Agent2 cannot see Agent1's requests
        async with TempUserManager(synced_test_db.admin, agent2_run_id) as agent2_creds:
            agent2_config = synced_test_db.admin.with_user(agent2_creds.username, agent2_creds.password)
            agent2_engine = create_engine(agent2_config.url())
            try:
                with Session(agent2_engine) as agent2_session:
                    visible_requests = agent2_session.query(LLMRequest).filter_by(agent_run_id=agent1_run_id).all()
                    assert len(visible_requests) == 0, "Unrelated agent should NOT see other agent's LLM requests"
            finally:
                agent2_engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
