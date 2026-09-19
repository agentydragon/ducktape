"""The BFF verifies a signed Dex token with independently configured login and target profiles."""

from collections.abc import AsyncIterator
from time import time

import httpx
import pytest
import pytest_bazel
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from util.net import bind_free_port
from util.testing.asgi import serve_app
from util.testing.mock_oidc import build_jwks, generate_rsa_keypair, sign_jwt
from agentplane.action_service.operator_oidc import OperatorOidcSettings, OperatorTokenProfile
from agentplane.app.action_federation import (
    DirectFederationSettings,
    FederatedOperatorActions,
    OperatorFederationError,
)
from agentplane.app.oidc import OIDCSettings, OperatorSession


@pytest.fixture
async def dex_session() -> AsyncIterator[OperatorSession]:
    private, public = generate_rsa_keypair()
    sock = bind_free_port()
    issuer = f"http://127.0.0.1:{sock.getsockname()[1]}"
    now = int(time())
    token = sign_jwt(
        private, {"iss": issuer, "aud": "test-dex-client", "sub": "test-dex-user", "iat": now, "exp": now + 60}
    )

    async def keys(_request: Request) -> JSONResponse:
        return JSONResponse(build_jwks(public))

    async with serve_app(Starlette(routes=[Route("/keys", keys)]), sock=sock):
        yield OperatorSession(
            issuer=issuer,
            subject="test-dex-user",
            username="test-dex-name",
            access_token=SecretStr(token),
            expires_at=now + 60,
        )


@pytest.mark.parametrize(
    ("login_profile", "target_profile", "accepted"),
    [
        (OperatorTokenProfile.DEX, OperatorTokenProfile.DEX, True),
        (OperatorTokenProfile.AUTHENTIK, OperatorTokenProfile.DEX, False),
        (OperatorTokenProfile.DEX, OperatorTokenProfile.AUTHENTIK, False),
    ],
)
async def test_direct_dex_token_requires_both_explicit_profiles(
    dex_session: OperatorSession,
    login_profile: OperatorTokenProfile,
    target_profile: OperatorTokenProfile,
    accepted: bool,
) -> None:
    oidc = OIDCSettings(
        issuer=dex_session.issuer,
        client_id="test-dex-client",
        client_secret="test-only-client-secret",
        session_secret="test-only-session-secret",
        public_base_url="http://test-app.invalid",
    )
    config = DirectFederationSettings(
        service_url="http://test-actions.invalid",
        login_jwks_uri=f"{dex_session.issuer}/keys",
        login_token_profile=login_profile,
        target=OperatorOidcSettings(
            issuer=dex_session.issuer,
            audience="test-dex-client",
            jwks_uri=f"{dex_session.issuer}/keys",
            token_profile=target_profile,
        ),
        scope="openid",
    )
    async with httpx.AsyncClient() as http:
        federation = FederatedOperatorActions(config, oidc, http)
        if accepted:
            assert dex_session.access_token is not None
            assert await federation.exchange(dex_session) == dex_session.access_token.get_secret_value()
        else:
            with pytest.raises(OperatorFederationError, match="operator_federation_token_invalid"):
                await federation.exchange(dex_session)


if __name__ == "__main__":
    pytest_bazel.main()
