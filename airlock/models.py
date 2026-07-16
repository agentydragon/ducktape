"""API models for the Airlock OAuth credential broker."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class ConnectedOAuthStatus(BaseModel):
    state: Literal["connected"] = Field(default="connected", description="Provider has a valid stored access token")
    expires_at: datetime = Field(description="Access token expiry timestamp")
    scope: str = Field(description="Provider-granted scope string stored with the access token")

    model_config = ConfigDict(extra="forbid")


class ExpiredOAuthStatus(BaseModel):
    """Token secret exists but the access token is past its expires_at.

    This means the refresh loop is running but failing to obtain a new token
    (e.g. the refresh token was revoked). Re-authorization is required.
    """

    state: Literal["expired"] = Field(default="expired", description="Stored access token has expired")
    expires_at: datetime = Field(description="Expired access token timestamp")
    scope: str = Field(description="Provider-granted scope string stored with the expired token")
    last_refresh_error: str | None = Field(
        default=None, description="Most recent error from the background refresh loop, if any."
    )

    model_config = ConfigDict(extra="forbid")


class DisconnectedOAuthStatus(BaseModel):
    state: Literal["disconnected"] = Field(default="disconnected", description="Provider has no stored token")

    model_config = ConfigDict(extra="forbid")


OAuthConnectionStatus = Annotated[
    ConnectedOAuthStatus | ExpiredOAuthStatus | DisconnectedOAuthStatus, Field(discriminator="state")
]


class DeploymentInfo(BaseModel):
    """Pod-level deployment metadata derived from the `AIRLOCK_IMAGE_TAG` env var."""

    image_tag: str | None = None
    source_commit: str | None = None
    source_commit_url: str | None = None

    model_config = ConfigDict(extra="forbid")


class OAuthProviderStatus(BaseModel):
    name: str = Field(description="Provider identifier")
    display_name: str = Field(description="Human-readable name")
    provider_type: str = Field(description="OAuth provider type.")
    requested_scopes: list[str] = Field(
        description=(
            "Scopes configured for this provider. Compared against the granted scope on the "
            "connected token to surface scope drift (re-auth required)."
        )
    )
    status: OAuthConnectionStatus = Field(description="Current token connection state for this provider")

    model_config = ConfigDict(extra="forbid")
