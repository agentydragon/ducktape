"""Session state across unavailable upstreams; the authenticated round trip is test_action_api."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_bazel
from fastapi import HTTPException
from starlette.requests import Request

from x.agentplane.action_service.client import CredentialPlaceholder, OperatorActionServiceClient
from x.agentplane.app.consent import ConsentAllow, ConsentDeny, decide_enrollment, preview_enrollment


async def test_lost_response_preserves_binding_and_exact_decision() -> None:
    previews: list[dict] = []
    decisions: list[dict] = []

    def downstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-only-token"
        if request.url.path.endswith("/identities"):
            return httpx.Response(200, json={"public_coder": {"enabled": True}})
        body = json.loads(request.content)
        if request.url.path.endswith("/preview"):
            previews.append(body)
            if len(previews) == 1:
                raise httpx.ReadError("lost preview response")
            return httpx.Response(
                200,
                json={
                    "client_id": "test",
                    "client_name": "test client",
                    "redirect_uri": "https://client.test/callback",
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    "version": 1,
                },
            )
        decisions.append(body)
        if len(decisions) == 1:
            raise httpx.ReadError("lost decision response")
        return httpx.Response(200, json={"verdict": "allow", "redirect_url": "https://idp.test/held"})

    browser = Request({"type": "http", "session": {}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(downstream), base_url="https://actions.test") as http:
        client = OperatorActionServiceClient(http, CredentialPlaceholder("test-only-token"))
        with pytest.raises(httpx.ReadError):
            await preview_enrollment(browser, "handle", client)
        preview = await preview_enrollment(browser, "handle", client)
        assert previews[0] == previews[1]
        decision = ConsentAllow(
            verdict="allow", csrf_token=preview.csrf_token, display_name="My client", identity_id="public_coder"
        )
        with pytest.raises(httpx.ReadError):
            await decide_enrollment(browser, "handle", decision, client)
        reloaded = await preview_enrollment(browser, "handle", client)
        assert reloaded.attempted_decision == decision
        with pytest.raises(HTTPException) as changed:
            await decide_enrollment(
                browser, "handle", ConsentDeny(verdict="deny", csrf_token=preview.csrf_token), client
            )
        assert changed.value.status_code == 409
        result = await decide_enrollment(browser, "handle", decision, client)
        assert decisions[0] == decisions[1]
        assert decisions[0]["browser_binding"] == previews[0]["browser_binding"]
        assert "csrf_token" not in decisions[0]
        assert result.redirect_url == "https://idp.test/held"


async def test_session_storage_is_bounded_and_expired_interactions_are_pruned() -> None:
    def downstream(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/identities"):
            return httpx.Response(200, json={})
        return httpx.Response(
            200,
            json={
                "client_id": "test",
                "client_name": None,
                "redirect_uri": "https://client.test/callback",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                "version": 1,
            },
        )

    browser = Request({"type": "http", "session": {}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(downstream), base_url="https://actions.test") as http:
        client = OperatorActionServiceClient(http, CredentialPlaceholder("test-only-token"))
        for index in range(32):
            await preview_enrollment(browser, str(index), client)
        with pytest.raises(HTTPException) as full:
            await preview_enrollment(browser, "overflow", client)
        assert full.value.status_code == 429
        browser.session["connection_enrollments"]["0"]["expires_at"] = 0
        with pytest.raises(HTTPException) as expired:
            await decide_enrollment(browser, "0", ConsentDeny(verdict="deny", csrf_token="expired"), client)
        assert expired.value.status_code == 403
        await preview_enrollment(browser, "replacement", client)
        assert "0" not in browser.session["connection_enrollments"]
        assert len(browser.session["connection_enrollments"]) == 32


if __name__ == "__main__":
    pytest_bazel.main()
