"""Request-bound Authentik JWT-bearer federation, following Haku hostexec's grant shape.

No global operator client/token cache, static BFF bearer, or workload-token promotion. Separate
providers use the same Authentik subject mode, and the app verifies subject continuity after exchange.

The login access token that the exchange presents is shorter-lived than the session holding it, so an
exchange first renews it with the login's refresh token when it is about to expire.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
import httpx2
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from agentplane.action_service.client import OperatorActionServiceClient
from agentplane.action_service.operator_oidc import OperatorOidcSettings, OperatorTokenProfile
from agentplane.app.identity import CallerIdentity, CallerKind
from agentplane.app.oidc import (
    CLIENT_NAME,
    LoginTokens,
    OIDCSettings,
    OperatorSession,
    TokenResponse,
    build_oauth,
    operator_session,
)
from agentplane.app.operator_sessions import SessionRow, operator_session_row
from mcp_infra.oidc_principal import InvalidOidcPrincipalError, OidcPrincipalVerificationUnavailableError

logger = logging.getLogger(__name__)


class OperatorFederationError(Exception):
    """Fixed public failure codes only; never provider bodies or token material."""

    def __init__(self, code: str, *, status_code: int = 403) -> None:
        super().__init__(code)
        self.status_code = status_code


class UpstreamFailure(BaseModel):
    """A failed upstream request -- to the identity provider or the Action Service -- without
    credentials, query, or provider text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: str
    url: str
    upstream_status: int | None = Field(description="The status answered, or None when no response arrived.")
    error_type: str = Field(description="The httpx exception class, which names the failure shape.")


def upstream_failure_detail(
    error: httpx.HTTPStatusError | httpx.RequestError | httpx2.HTTPStatusError | httpx2.TransportError,
) -> UpstreamFailure:
    return UpstreamFailure(
        method=error.request.method,
        url=str(error.request.url.copy_with(username="", password="", query=None, fragment=None)),
        upstream_status=error.response.status_code
        if isinstance(error, (httpx.HTTPStatusError, httpx2.HTTPStatusError))
        else None,
        error_type=type(error).__name__,
    )


async def _check_token_response(response: httpx2.Response) -> None:
    # Authlib otherwise discards the HTTP response when raising OAuthError.
    response.raise_for_status()


class _ActionFederationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service_url: str
    login_jwks_uri: str
    login_token_profile: OperatorTokenProfile = OperatorTokenProfile.AUTHENTIK
    target: OperatorOidcSettings
    scope: str = Field(min_length=1)

    @field_validator("service_url")
    @classmethod
    def service_endpoint(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
        ):
            raise ValueError("service_url must be an HTTP(S) URL without credentials, query, or fragment")
        return value


class ExchangeFederationSettings(_ActionFederationSettings):
    mode: Literal["exchange"] = "exchange"
    token_endpoint: str

    @field_validator("token_endpoint")
    @classmethod
    def secure_exchange_endpoint(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            not url.hostname
            or url.username is not None
            or url.password is not None
            or url.fragment
            or url.query
            or (url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost"}))
        ):
            raise ValueError("token_endpoint must be HTTPS (loopback HTTP is allowed for tests)")
        return value


class DirectFederationSettings(_ActionFederationSettings):
    mode: Literal["direct"] = "direct"
    token_endpoint: None = None


ActionFederationSettings = Annotated[ExchangeFederationSettings | DirectFederationSettings, Field(discriminator="mode")]


class FederatedOperatorActions:
    def __init__(self, config: ActionFederationSettings, oidc: OIDCSettings, http: httpx.AsyncClient) -> None:
        self._config = config
        self._http = http
        self._login_issuer = oidc.issuer
        self._renew_before = oidc.token_renew_before_seconds
        self._login = build_oauth(oidc).create_client(CLIENT_NAME)
        self._upstream = OperatorOidcSettings(
            issuer=oidc.issuer,
            audience=oidc.client_id,
            jwks_uri=config.login_jwks_uri,
            token_profile=config.login_token_profile,
        ).resolver()
        self._target = config.target.resolver()

    def for_request(self, request: Request) -> OperatorActionServiceClient:
        return OperatorActionServiceClient(self._http, _SessionToken(self, operator_session_row(request)))

    async def exchange(self, row: SessionRow) -> str:
        """A target token for the operator whose login `row` holds, renewing the login token first if
        it is about to expire."""
        session: OperatorSession | None = None
        try:
            session = await self._current_login(row)
            if session.issuer != self._login_issuer or session.tokens is None:
                raise OperatorFederationError("operator_reauthentication_required")
            if session.tokens.expires_at <= time.time():
                raise OperatorFederationError("operator_reauthentication_required", status_code=401)
            login_token = session.tokens.access_token.get_secret_value()
            upstream = await self._upstream.resolve({"access_token": login_token, "token_type": "Bearer"})
            if (upstream.issuer, upstream.subject) != (session.issuer, session.subject):
                raise OperatorFederationError("operator_federation_identity_mismatch")
            if self._config.mode == "direct":
                token = {"access_token": login_token, "token_type": "Bearer"}
            else:
                assert self._config.token_endpoint is not None
                # Authlib mutates token state: create a fresh OAuth client for each exchange.
                async with AsyncOAuth2Client(
                    client_id=self._config.target.audience,
                    timeout=10,
                    event_hooks={"response": [_check_token_response]},
                ) as client:
                    token = await client.fetch_token(
                        url=self._config.token_endpoint,
                        grant_type="client_credentials",
                        client_assertion_type="urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                        client_assertion=login_token,
                        scope=self._config.scope,
                    )
            downstream = await self._target.resolve(token)
            if downstream.subject != upstream.subject:
                raise OperatorFederationError("operator_federation_identity_mismatch")
            access_token = token["access_token"]
            if not isinstance(access_token, str):
                raise OperatorFederationError("operator_federation_token_invalid")
            return access_token
        except OperatorFederationError as error:
            subject = session.subject if session is not None else None
            logger.warning(f"operator federation refused for {subject=}: {error}")
            raise
        except InvalidOidcPrincipalError as error:
            logger.warning(f"operator federation rejected the login token: {type(error).__name__}")
            raise OperatorFederationError("operator_federation_token_invalid") from None
        except OidcPrincipalVerificationUnavailableError as error:
            if error.http_error is not None:
                logger.warning(
                    f"operator federation signing-key fetch failed: {upstream_failure_detail(error.http_error)}"
                )
                raise error.http_error from None
            logger.warning("operator federation cannot verify tokens: signing keys unavailable")
            raise OperatorFederationError("operator_federation_verification_unavailable", status_code=503) from None
        except (httpx.HTTPStatusError, httpx.RequestError, httpx2.HTTPStatusError, httpx2.TransportError) as error:
            logger.warning(f"operator federation exchange request failed: {upstream_failure_detail(error)}")
            raise
        except (OAuthError, ValueError) as error:
            # Provider text stays out of logs; the class names the failure shape.
            logger.warning(f"operator federation exchange failed: {type(error).__name__}")
            raise OperatorFederationError("operator_federation_exchange_failed", status_code=502) from None

    async def _current_login(self, row: SessionRow) -> OperatorSession:
        """The login as the row holds it now, its access token renewed if it is about to expire.

        Under the row lock, because the provider rotates the refresh token on use: a second replica
        spending the one it replaced would be refused, and would end the session. A refusal ends it
        here, so the browser's next request is sent to log in again.
        """
        async with row.locked() as payload:
            user = payload.get("user") if payload is not None else None
            session = OperatorSession.model_validate(user) if user else None
            tokens = session.tokens if session is not None else None
            if (
                payload is not None
                and session is not None
                and session.issuer == self._login_issuer
                and tokens is not None
                and tokens.refresh_token is not None
                and tokens.expires_at - time.time() <= self._renew_before
            ):
                try:
                    renewed = await self._renew(tokens.refresh_token)
                except OAuthError:
                    # Provider text stays out of logs; whichever error it answered, the grant is gone.
                    logger.warning(f"operator login renewal refused for {session.subject=}; ending the session")
                    payload.clear()
                    session = None
                else:
                    session = session.model_copy(update={"tokens": renewed})
                    payload["user"] = session.model_dump(mode="json")
        if session is None:
            raise OperatorFederationError("operator_reauthentication_required", status_code=401)
        return session

    async def _renew(self, refresh_token: SecretStr) -> LoginTokens:
        token = await self._login.fetch_access_token(
            grant_type="refresh_token", refresh_token=refresh_token.get_secret_value()
        )
        try:
            response = TokenResponse.model_validate(token)
        except ValidationError:
            raise OperatorFederationError("operator_federation_exchange_failed", status_code=502) from None
        if response.expires_at is None or response.expires_at <= time.time():
            raise OperatorFederationError("operator_federation_exchange_failed", status_code=502)
        # A provider that does not rotate the refresh token leaves the one just spent valid.
        return LoginTokens(
            access_token=response.access_token,
            expires_at=response.expires_at,
            refresh_token=response.refresh_token or refresh_token,
        )


class _SessionToken:
    def __init__(self, provider: FederatedOperatorActions, row: SessionRow) -> None:
        self._provider = provider
        self._row = row

    async def token(self) -> str:
        return await self._provider.exchange(self._row)


def operator_actions(request: Request, caller: CallerIdentity) -> OperatorActionServiceClient:
    """The request's operator-bound client, or the `OperatorFederationError` naming why it has none:
    the caller is not an operator session, federation is not configured, or the session has to log in
    again. The exchange itself happens at the first request the client makes."""
    if caller.kind is not CallerKind.OPERATOR:
        raise OperatorFederationError("operator_session_required")
    provider = request.app.state.operator_actions
    if provider is None:
        raise OperatorFederationError("operator_federation_not_configured", status_code=503)
    if not isinstance(provider, FederatedOperatorActions):
        raise TypeError("operator_actions must be FederatedOperatorActions")
    if operator_session(request) is None:
        raise OperatorFederationError("operator_reauthentication_required", status_code=401)
    return provider.for_request(request)
