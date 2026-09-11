"""Request-bound Authentik JWT-bearer federation, following Haku hostexec's grant shape.

No global operator client/token cache, static BFF bearer, or workload-token promotion. Separate
providers use the same Authentik subject mode, and the app verifies subject continuity after exchange.
"""

from __future__ import annotations

import time
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.httpx_client import AsyncOAuth2Client
from pydantic import BaseModel, ConfigDict, Field, field_validator

from mcp_infra.authentik_auth.oidc_principal import (
    AuthentikOidcPrincipalResolver,
    InvalidOidcPrincipalError,
    OidcPrincipalVerificationUnavailableError,
)
from x.agentplane.action_service.client import OperatorActionServiceClient
from x.agentplane.action_service.operator_oidc import OperatorOidcSettings
from x.agentplane.app.oidc import OIDCSettings, OperatorSession


class OperatorFederationError(Exception):
    """Fixed public failure codes only; never provider bodies or token material."""

    def __init__(self, code: str, *, status_code: int = 403) -> None:
        super().__init__(code)
        self.status_code = status_code


async def _check_token_response(response: httpx.Response) -> None:
    # Authlib otherwise discards the HTTP response when raising OAuthError.
    response.raise_for_status()


class _ActionFederationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service_url: str
    login_jwks_uri: str
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
        self._upstream = AuthentikOidcPrincipalResolver(
            expected_issuer=oidc.issuer,
            discovered_issuer=oidc.issuer,
            jwks_uri=config.login_jwks_uri,
            signing_algorithms=["RS256"],
            client_id=oidc.client_id,
        )
        self._target = config.target.resolver()

    def for_session(self, session: OperatorSession) -> OperatorActionServiceClient:
        return OperatorActionServiceClient(self._http, _SessionToken(self, session))

    async def exchange(self, session: OperatorSession) -> str:
        if session.issuer != self._login_issuer or session.expires_at <= time.time() or session.access_token is None:
            raise OperatorFederationError("operator_reauthentication_required")
        try:
            upstream = await self._upstream.resolve(
                {"access_token": session.access_token.get_secret_value(), "token_type": "Bearer"}
            )
            if (upstream.issuer, upstream.subject) != (session.issuer, session.subject):
                raise OperatorFederationError("operator_federation_identity_mismatch")
            if self._config.mode == "direct":
                token = {"access_token": session.access_token.get_secret_value()}
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
                        client_assertion=session.access_token.get_secret_value(),
                        scope=self._config.scope,
                    )
            downstream = await self._target.resolve(token)
            if downstream.subject != upstream.subject:
                raise OperatorFederationError("operator_federation_identity_mismatch")
            access_token = token["access_token"]
            if not isinstance(access_token, str):
                raise OperatorFederationError("operator_federation_token_invalid")
            return access_token
        except InvalidOidcPrincipalError:
            raise OperatorFederationError("operator_federation_token_invalid") from None
        except OidcPrincipalVerificationUnavailableError as error:
            if error.http_error is not None:
                raise error.http_error from None
            raise OperatorFederationError("operator_federation_verification_unavailable", status_code=503) from None
        except (OAuthError, ValueError):
            raise OperatorFederationError("operator_federation_exchange_failed", status_code=502) from None


class _SessionToken:
    def __init__(self, provider: FederatedOperatorActions, session: OperatorSession) -> None:
        self._provider = provider
        self._session = session

    async def token(self) -> str:
        return await self._provider.exchange(self._session)
