"""The BFF verifies a signed Dex token with independently configured login and target profiles, and
renews an expiring login token under the session row's lock."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import time
from urllib.parse import parse_qs

import httpx
import pytest
import pytest_bazel
from pydantic import SecretStr
from sqlalchemy import select, text
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agentplane.action_service.operator_oidc import OperatorOidcSettings, OperatorTokenProfile
from agentplane.app.action_federation import DirectFederationSettings, FederatedOperatorActions, OperatorFederationError
from agentplane.app.conftest import stored_login
from agentplane.app.database import connect
from agentplane.app.oidc import OIDCSettings
from agentplane.app.operator_sessions import (
    BrowserSession,
    LoginTokens,
    OperatorSession,
    OperatorSessionStore,
    SessionRow,
)
from util.net import bind_free_port
from util.testing.asgi import serve_app
from util.testing.mock_oidc import build_jwks, build_mock_oidc_app, generate_rsa_keypair, sign_jwt

CLIENT = "test-login-client"
SUBJECT = "test-login-subject"
SPENT = "test-spent-refresh-token"  # a test literal, not a real credential


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
            tokens=LoginTokens(
                access_token=SecretStr(token), expires_at=datetime.fromtimestamp(now + 60, UTC), refresh_token=None
            ),
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
    operator_sessions: OperatorSessionStore,
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
    row = await stored_login(operator_sessions, dex_session)
    async with httpx.AsyncClient() as http:
        federation = FederatedOperatorActions(config, oidc, http)
        if accepted:
            assert dex_session.tokens is not None
            assert await federation.exchange(row) == dex_session.tokens.access_token.get_secret_value()
        else:
            with pytest.raises(OperatorFederationError, match="operator_federation_token_invalid"):
                await federation.exchange(row)


@dataclass
class LoginProvider:
    federation: FederatedOperatorActions
    # A login whose access token has expired, holding the refresh token `SPENT`.
    session: OperatorSession
    # Every refresh token presented to the token endpoint, in order.
    presented: list[str] = field(default_factory=list)
    refuse: bool = False


@pytest.fixture
async def login_provider() -> AsyncIterator[LoginProvider]:
    """An Authentik-shaped login provider whose token endpoint renews by refresh token, rotating it,
    and a direct federation that hands the renewed login token on as the target token."""
    private, public = generate_rsa_keypair()
    sock = bind_free_port()
    issuer = f"http://127.0.0.1:{sock.getsockname()[1]}/application/o/test-login/"
    idp = build_mock_oidc_app(
        issuer_url=issuer, private_key=private, public_key=public, subject=SUBJECT, authentik_compatible=True
    )
    oidc = OIDCSettings(
        issuer=issuer,
        client_id=CLIENT,
        client_secret="test-only-client-secret",
        session_secret="test-only-session-secret",
        public_base_url="http://test-app.invalid",
    )
    config = DirectFederationSettings(
        service_url="http://test-actions.invalid",
        login_jwks_uri=f"{issuer}jwks/",
        target=OperatorOidcSettings(issuer=issuer, audience=CLIENT, jwks_uri=f"{issuer}jwks/"),
        scope="openid",
    )
    expired = LoginTokens(
        access_token=SecretStr("test-expired-login-token"),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        refresh_token=SecretStr(SPENT),
    )
    async with httpx.AsyncClient() as http:
        provider = LoginProvider(
            FederatedOperatorActions(config, oidc, http),
            OperatorSession(issuer=issuer, subject=SUBJECT, username="test-operator", tokens=expired),
        )

        async def renew(request: Request) -> JSONResponse:
            form = parse_qs((await request.body()).decode())
            assert form["grant_type"] == ["refresh_token"]
            provider.presented.append(form["refresh_token"][0])
            if provider.refuse:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            now = int(time())
            claims = {"iss": issuer, "aud": CLIENT, "azp": CLIENT, "sub": SUBJECT, "iat": now, "exp": now + 600}
            return JSONResponse(
                {
                    "access_token": sign_jwt(private, claims),
                    "token_type": "Bearer",
                    "expires_in": 600,
                    "refresh_token": f"test-rotated-refresh-token-{len(provider.presented)}",
                }
            )

        idp.routes.insert(0, Route("/application/o/token/", renew, methods=["POST"]))
        async with serve_app(idp, sock=sock):
            yield provider


async def _stored_tokens(store: OperatorSessionStore, row_id: str) -> LoginTokens | None:
    async with store.sessions() as db:
        row = await db.get(BrowserSession, row_id)
    assert row is not None
    login = row.login
    assert login is not None
    return login.tokens


async def _lock_waiters(store: OperatorSessionStore) -> int:
    async with store.sessions() as db:
        waiting = await db.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        )
    assert isinstance(waiting, int)
    return waiting


async def test_an_expired_login_token_is_renewed_and_the_rotated_one_kept(
    login_provider: LoginProvider, operator_sessions: OperatorSessionStore
) -> None:
    row = await stored_login(operator_sessions, login_provider.session)

    token = await login_provider.federation.exchange(row)

    renewed = await _stored_tokens(operator_sessions, row.id)
    assert renewed is not None
    assert renewed.refresh_token is not None
    assert login_provider.presented == [SPENT]
    assert renewed.refresh_token.get_secret_value() == "test-rotated-refresh-token-1"
    assert renewed.expires_at > datetime.now(UTC)
    assert token == renewed.access_token.get_secret_value()
    # Renewed, the token is good for another exchange without spending the refresh token again.
    assert await login_provider.federation.exchange(row) == token
    assert login_provider.presented == [SPENT]


async def test_two_replicas_renewing_one_login_spend_its_refresh_token_once(
    login_provider: LoginProvider, operator_sessions: OperatorSessionStore, db_url: str
) -> None:
    """The provider refuses a refresh token it has already rotated, so a second replica spending the
    same one would lose the session. The test holds the row until both replicas' exchanges queue on
    it, each having found the token due for renewal, so whichever renews second must see it renewed."""
    row = await stored_login(operator_sessions, login_provider.session)
    engine = connect(db_url)
    try:
        replica = SessionRow(OperatorSessionStore(engine), row.id, idle=timedelta(hours=1), step=timedelta(minutes=5))
        async with operator_sessions.sessions.begin() as holder:
            await holder.scalar(select(BrowserSession).where(BrowserSession.id == row.id).with_for_update())
            exchanges = [asyncio.create_task(login_provider.federation.exchange(each)) for each in (row, replica)]
            async with asyncio.timeout(30):
                while await _lock_waiters(operator_sessions) < 2:
                    if any(exchange.done() for exchange in exchanges):
                        raise AssertionError("an exchange finished without the row's lock")
                    await asyncio.sleep(0.02)
        tokens = await asyncio.gather(*exchanges)
    finally:
        await engine.dispose()

    renewed = await _stored_tokens(operator_sessions, row.id)
    assert renewed is not None
    assert renewed.refresh_token is not None
    assert login_provider.presented == [SPENT]
    assert renewed.refresh_token.get_secret_value() == "test-rotated-refresh-token-1"
    assert tokens == [renewed.access_token.get_secret_value()] * 2


async def test_a_refused_renewal_ends_the_session(
    login_provider: LoginProvider, operator_sessions: OperatorSessionStore
) -> None:
    login_provider.refuse = True
    row = await stored_login(operator_sessions, login_provider.session)

    with pytest.raises(OperatorFederationError, match="operator_reauthentication_required") as refused:
        await login_provider.federation.exchange(row)

    assert refused.value.status_code == 401
    async with operator_sessions.sessions() as db:
        assert await db.get(BrowserSession, row.id) is None
    # Gone, the session is not renewed again.
    with pytest.raises(OperatorFederationError, match="operator_reauthentication_required"):
        await login_provider.federation.exchange(row)
    assert login_provider.presented == [SPENT]


if __name__ == "__main__":
    pytest_bazel.main()
