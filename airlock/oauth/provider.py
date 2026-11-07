import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx2
from authlib.common.security import generate_token
from authlib.integrations.httpx_client import AsyncOAuth2Client
from pydantic import BaseModel, Field

from airlock.oauth.config import OAuth2ProviderConfig

logger = logging.getLogger(__name__)


ACCESS_TOKEN_FIELDS: set[str] = {"access_token", "token_type", "expires_at", "scope"}
ALL_TOKEN_FIELDS: set[str] = {"access_token", "refresh_token", "token_type", "expires_at", "scope"}


class TokenData(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = Field(default="Bearer")
    expires_at: datetime
    scope: str


class GenericOAuth2Provider:
    def __init__(
        self, config: OAuth2ProviderConfig, client_id: str, client_secret: str, default_redirect_uri: str
    ) -> None:
        self.config = config
        self.client_id = client_id
        self.client_secret = client_secret
        # Per-provider redirect_uri is legacy; new providers omit it and share one URL.
        self.redirect_uri = config.redirect_uri or default_redirect_uri

    def generate_state(self) -> str:
        return cast(str, generate_token(32))

    async def build_authorize_url(self, state: str) -> tuple[str, str | None]:
        code_verifier = generate_token(64) if self.config.use_pkce else None
        params = {
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.config.scopes),
            **self.config.extra_auth_params,
        }
        if self.config.aud is not None:
            params["aud"] = self.config.aud

        async with self._client() as client:
            url, _ = client.create_authorization_url(
                self.config.authorize_url, state=state, code_verifier=code_verifier, **params
            )
        return url, code_verifier

    async def exchange_code(self, code: str, code_verifier: str | None = None) -> TokenData:
        params: dict[str, Any] = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri}
        if code_verifier is not None:
            params["code_verifier"] = code_verifier
        async with self._client() as client:
            token = await client.fetch_token(self.config.token_url, **params)
        return _parse_token_response(token)

    async def refresh_tokens(self, refresh_token: str) -> TokenData:
        async with self._client() as client:
            token = await client.refresh_token(self.config.token_url, refresh_token=refresh_token)
        return _parse_token_response(token)

    def _client(self) -> AsyncOAuth2Client:
        client = AsyncOAuth2Client(
            client_id=self.client_id,
            client_secret=self.client_secret,
            token_endpoint_auth_method="client_secret_post",
            scope=" ".join(self.config.scopes),
            redirect_uri=self.redirect_uri,
            code_challenge_method="S256" if self.config.use_pkce else None,
        )
        client.register_compliance_hook("access_token_response", _raise_for_status)
        client.register_compliance_hook("refresh_token_response", _raise_for_status)
        return client

    def needs_refresh(self, token: TokenData) -> bool:
        margin = timedelta(seconds=self.config.refresh_margin_seconds)
        return datetime.now(UTC) >= token.expires_at - margin


def _raise_for_status(response: httpx2.Response) -> httpx2.Response:
    response.raise_for_status()
    return response


def _parse_token_response(data: Mapping[str, Any]) -> TokenData:
    expires_in = data.get("expires_in", 2592000)
    return TokenData(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", ""),
        token_type=data.get("token_type", "Bearer"),
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        scope=data.get("scope", ""),
    )
