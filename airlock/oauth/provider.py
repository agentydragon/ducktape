"""Generic OAuth2 provider: authorization URL, code exchange, token refresh."""

import base64
import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, Field

from airlock.oauth.config import OAuth2ProviderConfig

logger = logging.getLogger(__name__)


ACCESS_TOKEN_FIELDS: frozenset[str] = frozenset({"access_token", "token_type", "expires_at", "scope"})
ALL_TOKEN_FIELDS: frozenset[str] = frozenset({"access_token", "refresh_token", "token_type", "expires_at", "scope"})


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) per RFC 7636 with S256 method."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class TokenData(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = Field(default="Bearer")
    expires_at: datetime
    scope: str


class _BaseProvider:
    def generate_state(self) -> str:
        return secrets.token_urlsafe(32)


class GenericOAuth2Provider(_BaseProvider):
    def __init__(
        self, config: OAuth2ProviderConfig, client_id: str, client_secret: str, default_redirect_uri: str
    ) -> None:
        self.config = config
        self.client_id = client_id
        self.client_secret = client_secret
        # Per-provider redirect_uri is legacy; new providers omit it and share one URL.
        self.redirect_uri = config.redirect_uri or default_redirect_uri

    def build_authorize_url(self, state: str, code_challenge: str | None = None) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.config.scopes),
            "state": state,
            **self.config.extra_auth_params,
        }
        if code_challenge is not None:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        if self.config.aud is not None:
            params["aud"] = self.config.aud
        return f"{self.config.authorize_url}?{urlencode(params)}"

    async def exchange_code(self, code: str, code_verifier: str | None = None) -> TokenData:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
        }
        if code_verifier is not None:
            data["code_verifier"] = code_verifier
        async with httpx.AsyncClient() as client:
            response = await client.post(self.config.token_url, data=data)
            response.raise_for_status()
            return _parse_token_response(response.json())

    async def refresh_tokens(self, refresh_token: str) -> TokenData:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.config.token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            response.raise_for_status()
            token = _parse_token_response(response.json())
            # Google omits refresh_token on refresh responses — preserve the old one
            if not token.refresh_token:
                token = token.model_copy(update={"refresh_token": refresh_token})
            return token

    def needs_refresh(self, token: TokenData) -> bool:
        margin = timedelta(seconds=self.config.refresh_margin_seconds)
        return datetime.now(UTC) >= token.expires_at - margin


def _parse_token_response(data: dict) -> TokenData:
    expires_in = data.get("expires_in", 2592000)
    return TokenData(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", ""),
        token_type=data.get("token_type", "Bearer"),
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        scope=data.get("scope", ""),
    )
