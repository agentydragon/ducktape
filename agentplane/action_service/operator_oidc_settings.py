"""Pinned issuer and audience for Action Service operator tokens."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class OperatorTokenProfile(StrEnum):
    AUTHENTIK = "authentik"
    DEX = "dex"


class OperatorOidcSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str
    audience: str
    jwks_uri: str
    token_profile: OperatorTokenProfile = OperatorTokenProfile.AUTHENTIK
