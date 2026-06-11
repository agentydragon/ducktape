"""Pydantic models for the Claude OAuth profile API response."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

PROFILE_API_URL = "https://api.anthropic.com/api/oauth/profile"


class ProfileAccount(BaseModel):
    model_config = ConfigDict(extra="ignore")

    uuid: str
    full_name: str
    display_name: str
    email: str
    has_claude_max: bool
    has_claude_pro: bool
    created_at: datetime


class ProfileOrganization(BaseModel):
    model_config = ConfigDict(extra="ignore")

    uuid: str
    name: str
    organization_type: str
    billing_type: str
    rate_limit_tier: str
    has_extra_usage_enabled: bool
    subscription_status: str | None = None
    subscription_created_at: datetime | None = None


class ProfileApplication(BaseModel):
    model_config = ConfigDict(extra="ignore")

    uuid: str
    name: str
    slug: str


class ProfileResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    account: ProfileAccount
    organization: ProfileOrganization
    application: ProfileApplication
