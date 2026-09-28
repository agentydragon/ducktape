"""Session state across unavailable upstreams and concurrent interactions; the authenticated round
trip is test_action_api."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_bazel
from fastapi import HTTPException

from agentplane.action_service.client import CredentialPlaceholder, OperatorActionServiceClient
from agentplane.action_service.connections import NewConnection
from agentplane.app.action_federation import OperatorFederationError
from agentplane.app.conftest import stored_login
from agentplane.app.consent import ConsentAllow, ConsentDeny, decide_enrollment, preview_enrollment
from agentplane.app.operator_sessions import OperatorSession, OperatorSessionStore, SessionRow
from agentplane.subjects import ServiceAccountRef

PUBLIC_CODER = ServiceAccountRef(namespace="agentplane-test", name="test-public-coder")
LOGIN = OperatorSession(issuer="https://test-idp.invalid/", subject="test-subject", username="test-operator")


def _preview() -> dict[str, Any]:
    return {
        "client_id": "test",
        "client_name": "test client",
        "redirect_uri": "https://client.test/callback",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "version": 1,
    }


@asynccontextmanager
async def _client(downstream: httpx.MockTransport) -> AsyncIterator[OperatorActionServiceClient]:
    async with httpx.AsyncClient(transport=downstream, base_url="https://actions.test") as http:
        yield OperatorActionServiceClient(http, CredentialPlaceholder("test-only-token"))


async def _stored(row: SessionRow) -> dict[str, Any]:
    """The session's interactions as its row holds them."""
    async with row.locked() as held:
        interactions: dict[str, Any] = held.payload["connection_enrollments_v2"]
    return interactions


async def test_lost_response_preserves_binding_and_exact_decision(operator_sessions: OperatorSessionStore) -> None:
    previews: list[dict] = []
    decisions: list[dict] = []

    def downstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-only-token"
        if request.url.path.endswith("/caller-service-accounts"):
            return httpx.Response(200, json=[PUBLIC_CODER.model_dump()])
        if request.url.path.endswith("/connections"):
            return httpx.Response(200, json=[])
        body = json.loads(request.content)
        if request.url.path.endswith("/preview"):
            previews.append(body)
            if len(previews) == 1:
                raise httpx.ReadError("lost preview response")
            return httpx.Response(200, json=_preview())
        decisions.append(body)
        if len(decisions) == 1:
            raise httpx.ReadError("lost decision response")
        return httpx.Response(200, json={"verdict": "allow", "redirect_url": "https://idp.test/held"})

    row = await stored_login(operator_sessions, LOGIN)
    async with _client(httpx.MockTransport(downstream)) as client:
        with pytest.raises(httpx.ReadError):
            await preview_enrollment(row, "handle", client)
        preview = await preview_enrollment(row, "handle", client)
        assert previews[0] == previews[1]
        decision = ConsentAllow(
            verdict="allow",
            csrf_token=preview.csrf_token,
            connection=NewConnection(display_name="My client"),
            service_account=PUBLIC_CODER,
        )
        with pytest.raises(httpx.ReadError):
            await decide_enrollment(row, "handle", decision, client)
        reloaded = await preview_enrollment(row, "handle", client)
        assert reloaded.attempted_decision == decision
        with pytest.raises(HTTPException) as changed:
            await decide_enrollment(row, "handle", ConsentDeny(verdict="deny", csrf_token=preview.csrf_token), client)
        assert changed.value.status_code == 409
        result = await decide_enrollment(row, "handle", decision, client)
        assert decisions[0] == decisions[1]
        assert decisions[0]["browser_binding"] == previews[0]["browser_binding"]
        assert "csrf_token" not in decisions[0]
        assert result.redirect_url == "https://idp.test/held"


async def test_session_storage_is_bounded_and_expired_interactions_are_pruned(
    operator_sessions: OperatorSessionStore,
) -> None:
    def downstream(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("/caller-service-accounts", "/connections")):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=_preview())

    row = await stored_login(operator_sessions, LOGIN)
    async with _client(httpx.MockTransport(downstream)) as client:
        for index in range(32):
            await preview_enrollment(row, str(index), client)
        with pytest.raises(HTTPException) as full:
            await preview_enrollment(row, "overflow", client)
        assert full.value.status_code == 429
        async with row.locked() as held:
            held.payload["connection_enrollments_v2"]["0"]["expires_at"] = 0
        with pytest.raises(HTTPException) as expired:
            await decide_enrollment(row, "0", ConsentDeny(verdict="deny", csrf_token="expired"), client)
        assert expired.value.status_code == 403
        await preview_enrollment(row, "replacement", client)
    stored = await _stored(row)
    assert "0" not in stored
    assert len(stored) == 32


async def test_concurrent_previews_on_one_session_lose_no_interaction(operator_sessions: OperatorSessionStore) -> None:
    """Previews of one session interleaving, as two tabs' do, each keep their interaction, and two of
    one handle present one browser binding: the Action Service keeps the first it sees, so the
    session must not end up holding another."""
    bindings: defaultdict[str, set[str]] = defaultdict(set)
    parked: asyncio.Queue[None] = asyncio.Queue()
    release = asyncio.Event()

    async def downstream(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("/caller-service-accounts", "/connections")):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/decision"):
            return httpx.Response(200, json={"verdict": "deny", "redirect_url": None})
        bindings[request.url.path.split("/")[-2]].add(json.loads(request.content)["browser_binding"])
        parked.put_nowait(None)
        await release.wait()
        return httpx.Response(200, json=_preview())

    row = await stored_login(operator_sessions, LOGIN)
    async with _client(httpx.MockTransport(downstream)) as client:
        previews = [asyncio.create_task(preview_enrollment(row, handle, client)) for handle in ("one", "two", "two")]
        # Every preview has read and recorded its interaction before any records the Action
        # Service's answer.
        for _ in previews:
            await parked.get()
        release.set()
        one, two, two_again = await asyncio.gather(*previews)

        assert bindings.keys() == {"one", "two"}
        assert all(len(each) == 1 for each in bindings.values())
        assert two.csrf_token == two_again.csrf_token
        for handle, preview in (("one", one), ("two", two)):
            await decide_enrollment(row, handle, ConsentDeny(verdict="deny", csrf_token=preview.csrf_token), client)
    assert (await _stored(row)).keys() == {"one", "two"}


async def test_a_preview_on_an_ended_session_asks_for_a_login(operator_sessions: OperatorSessionStore) -> None:
    row = await stored_login(operator_sessions, LOGIN)
    await operator_sessions.end(row.id)

    async with _client(httpx.MockTransport(lambda _request: httpx.Response(500))) as client:
        with pytest.raises(OperatorFederationError, match="operator_reauthentication_required"):
            await preview_enrollment(row, "handle", client)


if __name__ == "__main__":
    pytest_bazel.main()
