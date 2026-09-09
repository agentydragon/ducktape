"""Real OAuth HTTP and PostgreSQL consent/grant authority, with a hermetic upstream IdP."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_bazel
from cryptography.fernet import Fernet
from fastapi import HTTPException
from key_value.aio.wrappers.base import BaseWrapper
from mcp.shared.auth import OAuthClientInformationFull
from mcp.types import CallToolResult
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette

from util.net import pick_free_port
from util.testing.asgi import serve_app
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair
from x.agentplane.action_service.api import create_app
from x.agentplane.action_service.auth import DisabledOperatorAuthenticator
from x.agentplane.action_service.catalog import ActionCatalog
from x.agentplane.action_service.connections import (
    ConnectionAuthority,
    GrantRejectedError,
    GrantStatus,
    Identity,
    NewConnection,
)
from x.agentplane.action_service.db import ActionStore, EnrollmentRow, make_sessionmaker
from x.agentplane.action_service.enrollments import (
    ConfirmedReconnectConnection,
    EnrollmentAllow,
    EnrollmentAuthority,
    EnrollmentConnection,
    EnrollmentPreviewInput,
)
from x.agentplane.action_service.models import ActionRequestView, CancellationResult, Executor, Principal, PrincipalRole
from x.agentplane.action_service.oauth import ActionsOAuthProxy, OAuthSettings, running_oauth
from x.agentplane.action_service.service import ActionService
from x.agentplane.action_service.updates import ActionUpdates
from x.agentplane.sandbox_auth.http import SandboxPrincipalAuthenticator

CALLBACK = "https://client.example.test/callback"
SCOPES = "openid email profile offline_access"
OPERATOR = Principal(issuer="https://operator.example.test/", subject="operator", role=PrincipalRole.OPERATOR)


@dataclass
class OAuthFixture:
    proxy: ActionsOAuthProxy
    connections: ConnectionAuthority
    enrollments: EnrollmentAuthority
    browser: httpx.AsyncClient
    base_url: str
    metadata: dict[str, str]
    settings: OAuthSettings

    async def register(self) -> str:
        response = await self.browser.post(
            self.metadata["registration_endpoint"],
            json={
                "client_name": "Example external harness",
                "redirect_uris": [CALLBACK],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": SCOPES,
            },
        )
        assert response.status_code == 201, response.text
        client_id = OAuthClientInformationFull.model_validate(response.json()).client_id
        assert client_id is not None
        return client_id

    async def authorize(self, client_id: str) -> tuple[str, str]:
        verifier = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        response = await self.browser.get(
            self.metadata["authorization_endpoint"],
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": CALLBACK,
                "scope": SCOPES,
                "state": "client-state-preserved",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": f"{self.base_url}/mcp",
            },
        )
        assert response.status_code == 302, response.text
        assert response.headers["location"].startswith("https://integration.example.test/#/connection-enrollments/")
        return response.headers["location"].rsplit("/", 1)[1], verifier

    async def approve(
        self, handle: str, *, connection: EnrollmentConnection | None = None, identity_id: str = "public-coder"
    ) -> str:
        binding = secrets.token_urlsafe(32)
        preview = await self.enrollments.preview(handle, EnrollmentPreviewInput(browser_binding=binding), OPERATOR)
        decision = await self.enrollments.decide(
            handle,
            EnrollmentAllow(
                browser_binding=binding,
                expected_version=preview.version,
                idempotency_key=secrets.token_urlsafe(16),
                connection=connection if connection is not None else NewConnection(display_name="Test external client"),
                identity_id=identity_id,
            ),
            OPERATOR,
        )
        assert decision.redirect_url is not None
        return decision.redirect_url

    async def callback(self, upstream_url: str) -> str:
        async with httpx.AsyncClient(follow_redirects=False) as upstream_browser:
            upstream = await upstream_browser.get(upstream_url)
        assert upstream.status_code == 302, upstream.text
        callback = await self.browser.get(upstream.headers["location"])
        assert callback.status_code == 302, callback.text
        destination = urlsplit(callback.headers["location"])
        assert f"{destination.scheme}://{destination.netloc}{destination.path}" == CALLBACK
        values = parse_qs(destination.query)
        assert values["state"] == ["client-state-preserved"]
        return values["code"][0]

    async def exchange(self, client_id: str, code: str, verifier: str) -> httpx.Response:
        return await self.browser.post(
            self.metadata["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": CALLBACK,
            },
        )


@pytest.fixture
async def oauth(engine: AsyncEngine, db_url: str, tmp_path: Path) -> AsyncIterator[OAuthFixture]:
    private_key, public_key = generate_rsa_keypair()
    oidc_port, service_port = pick_free_port(), pick_free_port()
    issuer = f"http://127.0.0.1:{oidc_port}/application/o/actions/"
    base_url = f"http://127.0.0.1:{service_port}"
    secret, signing, encryption = (tmp_path / name for name in ("upstream-secret", "jwt-key", "encryption-key"))
    secret.write_text("test-only-upstream-secret")
    signing.write_text(secrets.token_urlsafe(48))
    encryption.write_bytes(Fernet.generate_key())
    settings = OAuthSettings(
        config_url=f"{issuer}.well-known/openid-configuration",
        upstream_client_id="actions-upstream-client",
        upstream_client_secret_file=secret,
        base_url=base_url,
        integration_app_url="https://integration.example.test",
        jwt_signing_key_file=signing,
        encryption_key_file=encryption,
        upstream_issuer=issuer,
        upstream_subject="test-user",
        approving_operator=OPERATOR,
    )
    connections = ConnectionAuthority(make_sessionmaker(engine), {"public-coder": Identity(), "test-other": Identity()})
    enrollments = EnrollmentAuthority(make_sessionmaker(engine), connections)
    idp = build_mock_oidc_app(
        issuer_url=issuer, private_key=private_key, public_key=public_key, authentik_compatible=True
    )
    async with serve_app(idp, port=oidc_port), running_oauth(settings, db_url, enrollments, connections) as proxy:
        app = Starlette(routes=proxy.get_routes(mcp_path="/mcp"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=base_url, follow_redirects=False
        ) as browser:
            response = await browser.get(f"{base_url}/.well-known/oauth-authorization-server")
            response.raise_for_status()
            yield OAuthFixture(proxy, connections, enrollments, browser, base_url, response.json(), settings)


async def test_dcr_consent_pkce_refresh_and_revocation(oauth: OAuthFixture, db_url: str, engine: AsyncEngine) -> None:
    client_id = await oauth.register()
    assert await oauth.connections.list() == []  # Registration is not caller authority.
    handle, verifier = await oauth.authorize(client_id)
    assert await oauth.connections.list() == []
    upstream_url = await oauth.approve(handle)
    assert await oauth.connections.list() == []  # Consent alone is not an activated token family.
    code = await oauth.callback(upstream_url)
    wrong_pkce = await oauth.exchange(client_id, code, "incorrect-verifier")
    assert wrong_pkce.status_code == 401
    response = await oauth.exchange(client_id, code, verifier)
    assert response.status_code == 200, response.text
    tokens = response.json()
    grant = await oauth.proxy.authenticate(tokens["access_token"])
    assert grant is not None
    assert (grant.identity_id, grant.client_id) == ("public-coder", client_id)
    assert grant.principal().subject == "public-coder"
    assert (await oauth.exchange(client_id, code, verifier)).status_code == 401
    async with make_sessionmaker(engine)() as db:
        stored_values = list(await db.scalars(text("SELECT value::text FROM agentplane_oauth_kv")))
    assert stored_values
    assert all("Example external harness" not in value and '"access_token"' not in value for value in stored_values)
    # A replacement process uses the same encrypted PostgreSQL credentials and signing key.
    async with running_oauth(oauth.settings, db_url, oauth.enrollments, oauth.connections) as replacement:
        replacement.set_mcp_path("/mcp")
        restored = await replacement.authenticate(tokens["access_token"])
        assert restored is not None
        assert restored.provenance() == grant.provenance()

    refresh = await oauth.browser.post(
        oauth.metadata["token_endpoint"],
        data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": tokens["refresh_token"]},
    )
    assert refresh.status_code == 200, refresh.text
    refreshed = await oauth.proxy.authenticate(refresh.json()["access_token"])
    assert refreshed is not None
    assert refreshed.id == grant.id
    connection = await oauth.connections.get(grant.connection_id)
    await oauth.connections.unbind(connection.id, expected_version=connection.version)
    assert await oauth.proxy.authenticate(tokens["access_token"]) is None
    assert await oauth.proxy.authenticate(refresh.json()["access_token"]) is None
    refused = await oauth.browser.post(
        oauth.metadata["token_endpoint"],
        data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh.json()["refresh_token"]},
    )
    assert refused.status_code == 401


async def test_concurrent_code_exchange_issues_at_most_one_family(oauth: OAuthFixture) -> None:
    client_id = await oauth.register()
    handle, verifier = await oauth.authorize(client_id)
    code = await oauth.callback(await oauth.approve(handle))
    responses = await asyncio.gather(*(oauth.exchange(client_id, code, verifier) for _ in range(2)))
    assert sorted(response.status_code for response in responses) == [200, 401]
    assert len(await oauth.connections.list()) == 1


@pytest.mark.parametrize("identity_id", ["public-coder", "test-other"])
@pytest.mark.parametrize("fail_activation", [False, True])
async def test_fresh_oauth_reconnect_never_retargets_old_tokens(
    oauth: OAuthFixture, identity_id: str, fail_activation: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_client = await oauth.register()
    handle, verifier = await oauth.authorize(old_client)
    code = await oauth.callback(await oauth.approve(handle))
    old_tokens = (await oauth.exchange(old_client, code, verifier)).json()
    old_grant = await oauth.proxy.authenticate(old_tokens["access_token"])
    assert old_grant is not None
    connection = await oauth.connections.get(old_grant.connection_id)
    new_client = await oauth.register()
    handle, verifier = await oauth.authorize(new_client)
    code = await oauth.callback(
        await oauth.approve(
            handle,
            identity_id=identity_id,
            connection=ConfirmedReconnectConnection(
                connection_id=connection.id, expected_version=connection.version, authority_change_confirmed=True
            ),
        )
    )
    assert await oauth.proxy.authenticate(old_tokens["access_token"]) == old_grant
    with monkeypatch.context() as patch:
        if fail_activation:
            patch.setattr(
                oauth.connections, "activate", AsyncMock(side_effect=GrantRejectedError("test activation refused"))
            )
        result = await oauth.exchange(new_client, code, verifier)
    if fail_activation:
        assert result.status_code == 401, result.text
        new_grant = (await oauth.connections.get(connection.id)).grants[-1]
        assert new_grant.status == GrantStatus.PENDING
    else:
        assert result.status_code == 200, result.text
        verified = await oauth.proxy.authenticate(result.json()["access_token"])
        assert verified is not None
        new_grant = verified
    assert new_grant.identity_id == identity_id
    assert new_grant.client_id == new_client
    assert new_grant.connection_id == old_grant.connection_id
    assert new_grant.revision == old_grant.revision + 1
    assert await oauth.proxy.authenticate(old_tokens["access_token"]) is None
    refresh = await oauth.browser.post(
        oauth.metadata["token_endpoint"],
        data={"grant_type": "refresh_token", "client_id": old_client, "refresh_token": old_tokens["refresh_token"]},
    )
    assert refresh.status_code == 401
    assert (await oauth.exchange(new_client, code, verifier)).status_code == 401
    assert (await oauth.connections.get(connection.id)).grants[0].provenance() == old_grant.provenance()


async def test_stale_reconnect_code_is_invalid_grant_without_revoking_current_authority(oauth: OAuthFixture) -> None:
    client_id = await oauth.register()
    handle, verifier = await oauth.authorize(client_id)
    code = await oauth.callback(await oauth.approve(handle))
    tokens = (await oauth.exchange(client_id, code, verifier)).json()
    grant = await oauth.proxy.authenticate(tokens["access_token"])
    assert grant is not None
    connection = await oauth.connections.get(grant.connection_id)
    handle, verifier = await oauth.authorize(client_id)
    code = await oauth.callback(
        await oauth.approve(
            handle,
            connection=ConfirmedReconnectConnection(
                connection_id=connection.id, expected_version=connection.version, authority_change_confirmed=True
            ),
        )
    )
    await oauth.connections.rename(
        connection.id, expected_version=connection.version, display_name="Changed concurrently"
    )
    refused = await oauth.exchange(client_id, code, verifier)
    assert refused.status_code == 401, refused.text
    assert refused.json()["error"] == "invalid_grant"
    assert await oauth.proxy.authenticate(tokens["access_token"]) == grant


async def test_bearer_storage_outage_is_retryable_not_a_false_invalid_token(
    oauth: OAuthFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_id = await oauth.register()
    handle, verifier = await oauth.authorize(client_id)
    code = await oauth.callback(await oauth.approve(handle))
    response = await oauth.exchange(client_id, code, verifier)
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    assert isinstance(oauth.proxy._client_storage, BaseWrapper)
    with monkeypatch.context() as patch:
        patch.setattr(
            oauth.proxy._client_storage.key_value, "get", AsyncMock(side_effect=RuntimeError("test store outage"))
        )
        with pytest.raises(HTTPException) as error:
            await oauth.proxy.authenticate(token)
        assert error.value.status_code == 503
    assert await oauth.proxy.authenticate(token) is not None


@pytest.mark.parametrize("token_type", ["access_token", "refresh_token"])
async def test_token_revocation_ends_the_canonical_grant(oauth: OAuthFixture, token_type: str) -> None:
    client_id = await oauth.register()
    handle, verifier = await oauth.authorize(client_id)
    code = await oauth.callback(await oauth.approve(handle))
    response = await oauth.exchange(client_id, code, verifier)
    assert response.status_code == 200, response.text
    access_token = response.json()["access_token"]
    verified = await oauth.proxy.load_access_token(access_token)
    assert verified is not None
    assert verified.token == access_token
    other_client_id = await oauth.register()
    wrong_client = await oauth.browser.post(
        oauth.metadata["revocation_endpoint"],
        data={"client_id": other_client_id, "token": response.json()[token_type], "token_type_hint": token_type},
    )
    assert wrong_client.status_code == 200, wrong_client.text
    assert await oauth.proxy.authenticate(access_token) is not None
    revoked = await oauth.browser.post(
        oauth.metadata["revocation_endpoint"],
        data={"client_id": client_id, "token": response.json()[token_type], "token_type_hint": token_type},
    )
    assert revoked.status_code == 200, revoked.text
    assert await oauth.proxy.authenticate(access_token) is None
    refresh = await oauth.browser.post(
        oauth.metadata["token_endpoint"],
        data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": response.json()["refresh_token"]},
    )
    assert refresh.status_code == 401
    repeated = await oauth.browser.post(
        oauth.metadata["revocation_endpoint"],
        data={"client_id": client_id, "token": response.json()[token_type], "token_type_hint": token_type},
    )
    assert repeated.status_code == 200, repeated.text


async def test_one_registration_can_authorize_distinct_connections_to_same_identity(oauth: OAuthFixture) -> None:
    client_id = await oauth.register()
    grants = []
    for _ in range(2):
        handle, verifier = await oauth.authorize(client_id)
        code = await oauth.callback(await oauth.approve(handle))
        response = await oauth.exchange(client_id, code, verifier)
        assert response.status_code == 200, response.text
        grant = await oauth.proxy.authenticate(response.json()["access_token"])
        assert grant is not None
        grants.append(grant)
    assert grants[0].principal() == grants[1].principal()
    assert grants[0].connection_id != grants[1].connection_id
    assert grants[0].id != grants[1].id


async def test_upstream_login_without_approved_consent_cannot_issue_tokens(
    oauth: OAuthFixture, engine: AsyncEngine
) -> None:
    client_id = await oauth.register()
    _, verifier = await oauth.authorize(client_id)
    # Deliberately bypass the UI via test-only database inspection. Knowing the upstream
    # redirect, or successfully authenticating to the IdP, is not an approved grant.
    async with make_sessionmaker(engine)() as db:
        row = (await db.scalars(select(EnrollmentRow))).one()
        upstream_url = row.upstream_url
    code = await oauth.callback(upstream_url)
    assert (await oauth.exchange(client_id, code, verifier)).status_code == 401
    assert await oauth.connections.list() == []


async def test_consent_approver_must_match_verified_upstream_mapping(oauth: OAuthFixture) -> None:
    client_id = await oauth.register()
    handle, verifier = await oauth.authorize(client_id)
    browser_binding = secrets.token_urlsafe(32)
    different_operator = OPERATOR.model_copy(update={"subject": "another-operator"})
    preview = await oauth.enrollments.preview(
        handle, EnrollmentPreviewInput(browser_binding=browser_binding), different_operator
    )
    approved = await oauth.enrollments.decide(
        handle,
        EnrollmentAllow(
            browser_binding=browser_binding,
            expected_version=preview.version,
            idempotency_key="different-operator",
            connection=NewConnection(display_name="Wrong operator"),
            identity_id="public-coder",
        ),
        different_operator,
    )
    assert approved.redirect_url is not None
    code = await oauth.callback(approved.redirect_url)
    response = await oauth.exchange(client_id, code, verifier)
    assert response.status_code == 401
    assert await oauth.connections.list() == []


async def test_wrong_upstream_subject_is_refused_before_code_consumption(
    oauth: OAuthFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_id = await oauth.register()
    handle, verifier = await oauth.authorize(client_id)
    code = await oauth.callback(await oauth.approve(handle))
    with monkeypatch.context() as patch:
        patch.setattr(oauth.proxy, "_settings", oauth.settings.model_copy(update={"upstream_subject": "another-user"}))
        assert (await oauth.exchange(client_id, code, verifier)).status_code == 401
        assert await oauth.connections.list() == []
    # The pre-consumption check did not burn a valid code on the configuration mismatch.
    corrected = await oauth.exchange(client_id, code, verifier)
    assert corrected.status_code == 200, corrected.text


async def test_external_grant_reaches_canonical_mcp_admission_and_cancel(
    oauth: OAuthFixture, engine: AsyncEngine, db_url: str, echo_catalog: ActionCatalog, echo_executor: Executor
) -> None:
    client_id = await oauth.register()
    handle, verifier = await oauth.authorize(client_id)
    code = await oauth.callback(await oauth.approve(handle))
    issued = await oauth.exchange(client_id, code, verifier)
    assert issued.status_code == 200, issued.text
    bearer = issued.json()["access_token"]
    grant = await oauth.proxy.authenticate(bearer)
    assert grant is not None
    store = ActionStore(make_sessionmaker(engine), external_grants=oauth.connections)
    service = ActionService(store, echo_catalog, {"agentplane": echo_executor})
    sandbox = AsyncMock(spec=SandboxPrincipalAuthenticator, side_effect=HTTPException(401, "no workload credential"))
    app = create_app(
        service,
        sandbox,
        DisabledOperatorAuthenticator(),
        echo_catalog,
        updates=ActionUpdates(db_url),
        connections=oauth.connections,
        enrollments=oauth.enrollments,
        oauth=oauth.proxy,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=oauth.base_url) as http,
    ):
        unauthenticated = await http.post("/mcp", json={})
        assert unauthenticated.status_code == 401
        assert "resource_metadata=" in unauthenticated.headers["www-authenticate"]
        sandbox.reset_mock()
        request = {
            "idempotency_key": "external-original-key",
            "action": {"group": "agentplane", "name": "echo"},
            "arguments": {"message": "external-test"},
        }
        receipt = ActionRequestView.model_validate(
            await _call_mcp(http, bearer, "request_action", {"request": request})
        )
        assert receipt.external_grant == grant.provenance()
        assert receipt.caller_principal is None
        assert (await store.get(receipt.id, OPERATOR)).caller_principal == grant.principal().key
        cancelled = CancellationResult.model_validate(
            await _call_mcp(http, bearer, "cancel_action_request", {"request_id": str(receipt.id)})
        )
        assert cancelled.request.external_grant == grant.provenance()
        recovered = ActionRequestView.model_validate(
            await _call_mcp(http, bearer, "request_action", {"request": request})
        )
        assert recovered == cancelled.request
        await oauth.connections.revoke(grant.id)
        refused = await http.post("/mcp", headers={"Authorization": f"Bearer {bearer}"}, json={})
        assert refused.status_code == 401
        refresh_bearer = await http.post(
            "/mcp", headers={"Authorization": f"Bearer {issued.json()['refresh_token']}"}, json={}
        )
        assert refresh_bearer.status_code == 401
        sandbox.assert_not_awaited()  # Failed local OAuth credentials do not get forwarded to TokenReview.
    await service.close()


async def _call_mcp(http: httpx.AsyncClient, bearer: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = await http.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
    )
    assert response.status_code == 200, response.text
    data = next(line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: "))
    result = CallToolResult.model_validate(json.loads(data)["result"])
    assert not result.isError, result
    assert result.structuredContent is not None
    return result.structuredContent


if __name__ == "__main__":
    pytest_bazel.main()
