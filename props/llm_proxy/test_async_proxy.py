"""Exercise the proxy's async PostgreSQL path through FastAPI."""

from __future__ import annotations

import base64
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_bazel
from fastapi.testclient import TestClient
from sqlalchemy import select

from props.config import LLMProxyConfig, UpstreamConfig
from props.core.agent_types import FreeformTypeConfig
from props.db.async_database import AsyncDatabase
from props.db.database import Database
from props.db.models import AgentRun, AgentRunStatus, LLMRequest
from props.llm_proxy import routes
from props.llm_proxy.app import create_app
from props.orchestration.agent_credentials import ensure_agent_role
from props.testing.constants import DEFAULT_TEST_MODEL
from props.testing.fixtures.runs import FAKE_CRITIC_DIGEST, ensure_fake_agent_definitions

# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep //props/testing/fixtures:db
pytest_plugins = ("util.testing.postgres_fixtures", "props.testing.fixtures.db")


class FakeUpstream:
    async def __aenter__(self) -> FakeUpstream:
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        pass

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        assert url == "http://upstream.test/v1/responses"
        assert kwargs["json"]["model"] == DEFAULT_TEST_MODEL
        assert kwargs["headers"]["Authorization"] == "Bearer test-key"
        return httpx.Response(
            200, json={"id": "response-1", "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}}
        )


class TimeoutUpstream(FakeUpstream):
    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        raise httpx.TimeoutException("timed out")


def _authorization(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_async_auth_route_and_logging(synced_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = uuid4()
    with synced_db.session() as session:
        ensure_fake_agent_definitions(session)
        session.add(
            AgentRun(
                agent_run_id=run_id,
                image_digest=FAKE_CRITIC_DIGEST,
                model=DEFAULT_TEST_MODEL,
                type_config=FreeformTypeConfig(),
                status=AgentRunStatus.IN_PROGRESS,
                budget_usd=5.0,
            )
        )
    monkeypatch.setenv("PROPS_AGENT_PASSWORD_SALT", "proxy-test-salt")
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    credentials = await ensure_agent_role(synced_db.config, run_id)
    monkeypatch.setattr(routes.httpx, "AsyncClient", FakeUpstream)

    config = LLMProxyConfig(
        upstreams={"test": UpstreamConfig(url="http://upstream.test/v1", api_key_env="TEST_API_KEY")}
    )
    async with AsyncDatabase(synced_db.config) as proxy_db:
        with TestClient(create_app(db=proxy_db, config=config)) as client:
            bad_auth = _authorization(credentials.username, "incorrect")
            assert client.post("/v1/responses", headers=bad_auth, json={"model": DEFAULT_TEST_MODEL}).status_code == 401

            response = client.post(
                "/v1/responses",
                headers=_authorization(credentials.username, credentials.password),
                json={"model": DEFAULT_TEST_MODEL, "input": "hello"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["id"] == "response-1"

            monkeypatch.setattr(routes.httpx, "AsyncClient", TimeoutUpstream)
            timeout_response = client.post(
                "/v1/responses",
                headers=_authorization(credentials.username, credentials.password),
                json={"model": DEFAULT_TEST_MODEL, "input": "second"},
            )
            assert timeout_response.status_code == 504

        async with proxy_db.session() as session:
            requests = (await session.scalars(select(LLMRequest).where(LLMRequest.agent_run_id == run_id))).all()
            assert len(requests) == 2
            successful = next(request for request in requests if request.error is None)
            assert successful.input_tokens == 3
            assert successful.output_tokens == 2
            assert successful.request_body["input"] == "hello"
            timed_out = next(request for request in requests if request.error == "Upstream timeout")
            assert timed_out.request_body["input"] == "second"


if __name__ == "__main__":
    pytest_bazel.main()
