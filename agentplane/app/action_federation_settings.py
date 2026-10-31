"""App-owned operator-action federation settings."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from agentplane.action_service.operator_oidc_settings import OperatorOidcSettings, OperatorTokenProfile
from util.urls import HttpEndpointUrl, HttpsOrLoopbackHttpEndpointUrl


class _ActionFederationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service_url: HttpEndpointUrl
    login_jwks_uri: str
    login_token_profile: OperatorTokenProfile = OperatorTokenProfile.AUTHENTIK
    target: OperatorOidcSettings
    scope: str = Field(min_length=1)


class ExchangeFederationSettings(_ActionFederationSettings):
    mode: Literal["exchange"] = "exchange"
    token_endpoint: HttpsOrLoopbackHttpEndpointUrl


class DirectFederationSettings(_ActionFederationSettings):
    mode: Literal["direct"] = "direct"
    token_endpoint: None = None


ActionFederationSettings = Annotated[ExchangeFederationSettings | DirectFederationSettings, Field(discriminator="mode")]
