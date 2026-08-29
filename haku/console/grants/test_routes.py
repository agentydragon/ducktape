"""HTTP contract for unified grant inspection and revocation."""

from __future__ import annotations

import datetime
import json
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from functools import partial
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.config import KubernetesAuthorizationConfig, KubernetesAuthorizationSubject
from haku.console.conftest import DEFAULT_ACCESS_PROFILE_ID, default_agent_binding, insert_approved_tool_call
from haku.console.grants.catalog import GrantCatalog
from haku.console.grants.http.models import (
    GrantSpec as HttpGrantSpec,
    HttpMethod,
    HttpOrigin,
    HttpRequestCoverage,
    HttpScheme,
)
from haku.console.grants.http.service import GrantService as HttpGrantService
from haku.console.grants.kubernetes.authorization import KubernetesSubjectAccessReviewClient
from haku.console.grants.kubernetes.models import Grant, NamespacesGrantScope, Rule
from haku.console.grants.kubernetes.service import GrantService as KubernetesGrantService
from haku.console.grants.principal import AgentGrantPrincipal

# TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
# gazelle:include_dep @pypi//httpx

_SUBJECT = KubernetesAuthorizationSubject(username="system:serviceaccount:console-test:agent")


@dataclass(frozen=True, slots=True)
class _Console:
    """One production-shaped console app over a fresh migrated database."""

    client: TestClient
    sessions: async_sessionmaker[AsyncSession]
    grants: KubernetesGrantService
    http_grants: HttpGrantService
    agent_id: UUID
    binding_id: UUID

    def call[T](self, func: Callable[..., Awaitable[T]], *args: Any) -> T:
        """Run one async step on the app's own event loop, where its engine lives."""
        assert self.client.portal is not None
        return self.client.portal.call(func, *args)


@pytest.fixture
def console(make_operator_client: Callable[..., Any]) -> Iterator[_Console]:
    # The default static Agent is owned by this configured external identity.
    with make_operator_client(operator_external_user_key="default-op") as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        kubernetes_grants = cast(KubernetesGrantService, app.state.kubernetes_grants)
        # The regular app fixture has no Kubernetes config. Install the production catalog with a
        # config-file authority so this route exercises both catalog sources without duplicating
        # its database projection in a test double.
        app.state.grant_catalog = GrantCatalog(
            kubernetes_grants=kubernetes_grants,
            http_grants=cast(HttpGrantService, app.state.http_grants),
            kubernetes_config=KubernetesAuthorizationConfig(
                subjects_by_access_profile={DEFAULT_ACCESS_PROFILE_ID: _SUBJECT}
            ),
            sar_client=KubernetesSubjectAccessReviewClient(),
        )
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        yield _Console(
            client=client,
            sessions=sessions,
            grants=kubernetes_grants,
            http_grants=cast(HttpGrantService, app.state.http_grants),
            agent_id=agent_id,
            binding_id=binding_id,
        )


def _seed_grant(console: _Console) -> Grant:
    source_tool_call_id = console.call(
        partial(
            insert_approved_tool_call,
            console.sessions,
            binding_id=console.binding_id,
            now=datetime.datetime.now(datetime.UTC),
        )
    )

    async def create() -> Grant:
        return await console.grants.create_grant(
            owner_agent_id=console.agent_id,
            grant_principal=AgentGrantPrincipal(agent_id=console.agent_id),
            source_tool_call_id=source_tool_call_id,
            scope=NamespacesGrantScope(namespaces={"public-coder-agent"}),
            rules=(Rule(api_groups={""}, resources={"pods/log"}, verbs={"get"}),),
            expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=25),
        )

    return console.call(create)


def _seed_http_grant(console: _Console) -> UUID:
    source_tool_call_id = console.call(
        partial(
            insert_approved_tool_call,
            console.sessions,
            binding_id=console.binding_id,
            now=datetime.datetime.now(datetime.UTC),
            server_id="grants",
        )
    )

    async def create() -> UUID:
        (grant,) = await console.http_grants.create_grants(
            owner_agent_id=console.agent_id,
            grant_principal=AgentGrantPrincipal(agent_id=console.agent_id),
            source_tool_call_id=source_tool_call_id,
            grants=(
                HttpGrantSpec(
                    origin=HttpOrigin(scheme=HttpScheme.HTTPS, host="grocy.example", port=443),
                    coverage=HttpRequestCoverage(methods=frozenset({HttpMethod.GET})),
                ),
            ),
            expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=25),
        )
        return grant.grant_id

    return console.call(create)


def _wire(instant: datetime.datetime) -> str:
    return instant.isoformat().replace("+00:00", "Z")


def test_lists_direct_catalog_grants_with_provenance(console: _Console) -> None:
    grant = _seed_grant(console)
    assert grant.expires_at is not None

    response = console.client.get("/api/grants")

    assert response.status_code == 200
    record = next(record for record in response.json()["grants"] if record["source"]["kind"] == "database")
    assert record == {
        "source": {
            "kind": "database",
            "id": str(grant.grant_id),
            "tool_call_id": grant.source_tool_call_id,
            "created_at": _wire(grant.created_at),
        },
        "subject": {"kind": "agent", "agent_id": str(console.agent_id)},
        "coverage": {
            "kind": "kubernetes_rules",
            "scope": {"kind": "namespaces", "namespaces": ["public-coder-agent"]},
            "rules": [
                {
                    "api_groups": [""],
                    "resources": ["pods/log"],
                    "verbs": ["get"],
                    "resource_names": [],
                    "non_resource_urls": [],
                }
            ],
        },
        "validity": {"ends_at": _wire(grant.expires_at), "status": "active", "ended_at": None, "end_reason": None},
    }
    config_record = next(record for record in response.json()["grants"] if record["source"]["kind"] == "config_file")
    assert config_record["source"] == {
        "kind": "config_file",
        "entry_id": f"kubernetes-profile:{DEFAULT_ACCESS_PROFILE_ID}",
    }
    assert config_record["validity"] == {"ends_at": None, "status": "active", "ended_at": None, "end_reason": None}


def test_revoke_accepts_a_blank_reason(console: _Console) -> None:
    grant = _seed_grant(console)
    path = "/api/grants/revoke"

    assert (
        console.client.post(
            path,
            json={"grant_ids": [str(grant.grant_id)], "reason": "risk"},
            headers={"Origin": "https://untrusted.test"},
        ).status_code
        == 403
    )
    headers = {"Origin": "https://haku.test"}
    response = console.client.post(path, json={"grant_ids": [str(grant.grant_id)], "reason": "   "}, headers=headers)

    assert response.status_code == 200
    assert response.json()["grants"][0]["validity"]["status"] == "ended"
    assert response.json()["grants"][0]["validity"]["end_reason"] is None


def test_lists_http_database_grants_through_the_generic_route(console: _Console) -> None:
    grant_id = _seed_http_grant(console)

    response = console.client.get("/api/grants")

    assert response.status_code == 200
    assert {
        record["source"]["id"]
        for record in response.json()["grants"]
        if record["source"]["kind"] == "database" and record["coverage"]["kind"] == "http"
    } == {str(grant_id)}


def test_lists_one_exact_declared_principal_when_requested(console: _Console) -> None:
    grant = _seed_grant(console)

    response = console.client.get(
        "/api/grants", params={"principal": json.dumps({"kind": "agent", "agent_id": str(console.agent_id)})}
    )

    assert response.status_code == 200
    assert [record["source"]["id"] for record in response.json()["grants"]] == [str(grant.grant_id)]


def test_rejects_a_non_principal_list_filter(console: _Console) -> None:
    response = console.client.get("/api/grants", params={"principal": json.dumps({"kind": "agent"})})

    assert response.status_code == 422


def test_revoke_grants_uses_durable_grant_ids(console: _Console) -> None:
    first = _seed_grant(console)
    second = _seed_grant(console)
    path = "/api/grants/revoke"
    headers = {"Origin": "https://haku.test"}

    assert (
        console.client.post(
            path,
            json={"grant_ids": [str(first.grant_id), str(second.grant_id)], "reason": "risk"},
            headers={"Origin": "https://untrusted.test"},
        ).status_code
        == 403
    )
    assert console.client.post(path, json={"grant_ids": [], "reason": "risk"}, headers=headers).status_code == 422
    assert (
        console.client.post(path, json={"grant_ids": [str(uuid4())], "reason": "risk"}, headers=headers).status_code
        == 404
    )

    response = console.client.post(
        path,
        json={"grant_ids": [str(first.grant_id), str(second.grant_id)], "reason": "  pilot complete  "},
        headers=headers,
    )

    assert response.status_code == 200
    assert {item["source"]["id"] for item in response.json()["grants"]} == {str(first.grant_id), str(second.grant_id)}
    assert [item["validity"]["status"] for item in response.json()["grants"]] == ["ended", "ended"]
    assert [item["validity"]["end_reason"] for item in response.json()["grants"]] == [
        "pilot complete",
        "pilot complete",
    ]


if __name__ == "__main__":
    pytest_bazel.main()
