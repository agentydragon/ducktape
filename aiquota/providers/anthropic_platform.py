"""Anthropic Platform credits fetched through the private session proxy."""

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

from aiquota.models import (
    AnthropicPlatformCredits,
    AnthropicPlatformCreditValue,
    AnthropicPlatformOrganization,
    AnthropicPlatformTranche,
    FetchError,
    FetchSuccess,
    ProviderFetch,
)
from aiquota.providers.base import Provider
from aiquota.providers.client import ProviderClientFactory

API_TIMEOUT_SECS = 20.0


class AnthropicPlatformSettings(BaseModel):
    enabled: bool = False
    url: str | None = None
    token_path: Path | None = None


class _Organization(BaseModel):
    uuid: str
    name: str
    capabilities: list[str]

    model_config = ConfigDict(hide_input_in_errors=True)


class _CreditAmount(BaseModel):
    amount_minor: int
    exponent: int

    model_config = ConfigDict(hide_input_in_errors=True)


class _CreditValue(BaseModel):
    credits: _CreditAmount | None = None
    money: object | None = None

    model_config = ConfigDict(hide_input_in_errors=True)


class _Product(BaseModel):
    product: str
    model_family: str | None = None
    model_version: str | None = None

    model_config = ConfigDict(hide_input_in_errors=True)


class _Scope(BaseModel):
    kind: str
    applies_to: list[_Product] = Field(default_factory=list)

    model_config = ConfigDict(hide_input_in_errors=True)


class _Tranche(BaseModel):
    id: str
    name: str | None = None
    currency: str
    granted_at: datetime
    expires_at: datetime | None = None
    granted: _CreditValue
    remaining: _CreditValue
    scope: _Scope

    model_config = ConfigDict(hide_input_in_errors=True)

    def to_model(self) -> AnthropicPlatformTranche:
        return AnthropicPlatformTranche.model_validate(self.model_dump())


class _CreditsResponse(BaseModel):
    currency: str
    balance: _CreditValue
    tranches: list[_Tranche]
    promo_tranches: list[_Tranche]

    model_config = ConfigDict(hide_input_in_errors=True)

    def to_organization(self, organization: _Organization) -> AnthropicPlatformOrganization:
        return AnthropicPlatformOrganization(
            uuid=organization.uuid,
            name=organization.name,
            currency=self.currency,
            balance=AnthropicPlatformCreditValue.model_validate(self.balance.model_dump()),
            tranches=[tranche.to_model() for tranche in self.tranches],
            promo_tranches=[tranche.to_model() for tranche in self.promo_tranches],
        )


class AnthropicPlatformProvider(Provider):
    name = "anthropic_platform"

    def __init__(self, settings: AnthropicPlatformSettings, client_factory: ProviderClientFactory) -> None:
        self.settings = settings
        self.client_factory = client_factory

    async def fetch(self) -> ProviderFetch:
        now = datetime.now(UTC)
        if not self.settings.url:
            return ProviderFetch(
                fetched_at=now, result=FetchError(error="Anthropic Platform proxy URL is not configured")
            )
        base_url = self.settings.url
        try:
            token = (
                self.settings.token_path.expanduser().read_text().strip()
                if self.settings.token_path
                else os.environ.get("AIQUOTA_ANTHROPIC_PLATFORM_TOKEN", "")
            )
            if not token:
                raise ValueError("Anthropic Platform proxy token is not configured")
            organizations_url = base_url.rstrip("/") + "/api/organizations"
            headers = {"Authorization": f"Bearer {token}"}
            async with self.client_factory(self.name, {organizations_url}, API_TIMEOUT_SECS) as client:
                response = await client.get(organizations_url, headers=headers)
            response.raise_for_status()
            raw_organizations = response.json()
            if not isinstance(raw_organizations, list):
                raise ValueError("Anthropic Platform organization response must be a JSON array")
            organizations = [_Organization.model_validate(item) for item in raw_organizations]
        except (httpx.HTTPError, OSError, ValueError) as exc:
            return ProviderFetch(
                fetched_at=now, result=FetchError(error=f"Anthropic Platform orgs: {_error_message(exc)}")
            )

        api_organizations = [organization for organization in organizations if "api" in organization.capabilities]
        if not api_organizations:
            return ProviderFetch(
                fetched_at=now,
                result=FetchSuccess(anthropic_platform_credits=AnthropicPlatformCredits(organizations=[])),
            )

        results = await asyncio.gather(
            *(self._fetch_organization(organization, token, base_url) for organization in api_organizations)
        )
        return ProviderFetch(
            fetched_at=now,
            result=FetchSuccess(anthropic_platform_credits=AnthropicPlatformCredits(organizations=results)),
        )

    async def _fetch_organization(
        self, organization: _Organization, token: str, base_url: str
    ) -> AnthropicPlatformOrganization:
        url = base_url.rstrip("/") + f"/api/organizations/{quote(organization.uuid, safe='')}/prepaid/credits"
        try:
            async with self.client_factory(self.name, {url}, API_TIMEOUT_SECS) as client:
                response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
            credits = _CreditsResponse.model_validate_json(response.content)
            return credits.to_organization(organization)
        except (httpx.HTTPError, ValueError) as exc:
            return AnthropicPlatformOrganization(
                uuid=organization.uuid, name=organization.name, error=_error_message(exc)
            )


def _error_message(exc: Exception) -> str:
    """Report the upstream status without displaying its potentially sensitive body."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"upstream rejected request (HTTP {exc.response.status_code})"
    return str(exc).strip() or type(exc).__name__
