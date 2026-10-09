"""App-owned operator-action federation settings."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, AnyHttpUrl, BaseModel, ConfigDict, Field, TypeAdapter, UrlConstraints

from agentplane.action_service.operator_oidc_settings import OperatorOidcSettings, OperatorTokenProfile
from util.urls import HttpsOrLocalhostHttpUrl


def _validate_action_service_url(value: AnyHttpUrl) -> AnyHttpUrl:
    if (
        value.username is not None
        or value.password is not None
        or value.query is not None
        or value.fragment is not None
    ):
        raise ValueError("service_url must be an HTTP(S) URL without credentials, query, or fragment")
    return value


ActionServiceUrl = Annotated[
    AnyHttpUrl,
    UrlConstraints(allowed_schemes=["http", "https"], host_required=True, preserve_empty_path=True),
    AfterValidator(_validate_action_service_url),
]
_ACTION_SERVICE_URL: TypeAdapter[ActionServiceUrl] = TypeAdapter(ActionServiceUrl)


def parse_action_service_url(value: str) -> ActionServiceUrl:
    """Construct the app's service URL without normalizing a host-only URL to add `/`."""
    return _ACTION_SERVICE_URL.validate_python(value)


class _ActionFederationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service_url: ActionServiceUrl
    login_jwks_uri: str
    login_token_profile: OperatorTokenProfile = OperatorTokenProfile.AUTHENTIK
    target: OperatorOidcSettings
    scope: str = Field(min_length=1)


class ExchangeFederationSettings(_ActionFederationSettings):
    mode: Literal["exchange"] = "exchange"
    token_endpoint: HttpsOrLocalhostHttpUrl


class DirectFederationSettings(_ActionFederationSettings):
    mode: Literal["direct"] = "direct"
    token_endpoint: None = None


ActionFederationSettings = Annotated[ExchangeFederationSettings | DirectFederationSettings, Field(discriminator="mode")]
