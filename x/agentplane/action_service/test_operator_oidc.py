"""Signed tokens hit the actual operator API; valid JWTs still need explicit authorization."""

from __future__ import annotations

import time
from typing import Any, cast

import httpx
import pytest
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine

from util.net import pick_free_port
from util.testing.asgi import serve_app
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair, sign_jwt
from x.agentplane.action_service.api import create_app
from x.agentplane.action_service.catalog import ActionCatalog
from x.agentplane.action_service.db import ActionStore, make_sessionmaker
from x.agentplane.action_service.operator_oidc import OidcOperatorAuthenticator, OperatorOidcSettings
from x.agentplane.action_service.service import ActionService
from x.agentplane.action_service.updates import ActionUpdates
from x.agentplane.sandbox_auth.http import SandboxPrincipalAuthenticator


@pytest.mark.parametrize(
    "failure", [None, "issuer", "audience", "expired", "unauthorized", "signature", "azp", "missing-sub"]
)
async def test_signed_operator_admission(engine: AsyncEngine, failure: str | None) -> None:
    private, public = generate_rsa_keypair()
    port = pick_free_port()
    issuer = f"http://127.0.0.1:{port}"
    idp = build_mock_oidc_app(issuer_url=issuer, private_key=private, public_key=public)
    catalog = ActionCatalog()
    service = ActionService(ActionStore(make_sessionmaker(engine)), catalog, {})
    app = create_app(
        service,
        cast(SandboxPrincipalAuthenticator, None),
        OidcOperatorAuthenticator(
            OperatorOidcSettings(
                issuer=issuer,
                audience="actions",
                jwks_uri=f"{issuer}/jwks",
                subjects=frozenset({"operator-a", "operator-b"}),
            )
        ),
        catalog,
        updates=ActionUpdates("postgresql://unused-test-listener"),
    )
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": issuer,
        "aud": "actions",
        "azp": "actions",
        "sub": "operator-a",
        "iat": now,
        "exp": now + 60,
    }
    if failure == "issuer":
        claims["iss"] = "https://wrong-issuer.invalid"
    elif failure == "audience":
        claims["aud"] = "workload"
    elif failure == "expired":
        claims.update(iat=now - 600, exp=now - 300)
    elif failure == "unauthorized":
        claims["sub"] = "signed-but-not-authorized"
    elif failure == "signature":
        private, _ = generate_rsa_keypair()
    elif failure == "azp":
        claims["azp"] = "machine-client"
    elif failure == "missing-sub":
        del claims["sub"]
    async with (
        serve_app(idp, port=port),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://actions.test") as client,
    ):
        response = await client.get(
            "/v1/operator/action-requests", headers={"Authorization": f"Bearer {sign_jwt(private, claims)}"}
        )
        assert response.status_code == (200 if failure is None else 401)
        assert (await client.get("/v1/operator/action-requests")).status_code == 401


if __name__ == "__main__":
    pytest_bazel.main()
