"""App-owned operator-action federation settings."""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentplane.action_service.operator_oidc_settings import OperatorOidcSettings, OperatorTokenProfile
from util.urls import HttpsOrLocalhostHttpUrlString


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
    token_endpoint: HttpsOrLocalhostHttpUrlString


class DirectFederationSettings(_ActionFederationSettings):
    mode: Literal["direct"] = "direct"
    token_endpoint: None = None


ActionFederationSettings = Annotated[ExchangeFederationSettings | DirectFederationSettings, Field(discriminator="mode")]
