"""OpenAI Platform credit grants fetched through the private session proxy."""

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
from pydantic import BaseModel

from aiquota.models import FetchError, FetchSuccess, OpenAIPlatformCreditGrant, OpenAIPlatformCredits, ProviderFetch
from aiquota.providers.base import Provider
from aiquota.providers.client import ProviderClientFactory


class OpenAIPlatformSettings(BaseModel):
    enabled: bool = False
    url: str | None = None
    token_path: Path | None = None


class _Grant(BaseModel):
    grant_amount: Decimal
    used_amount: Decimal
    effective_at: datetime
    expires_at: datetime | None = None
    is_scheduled: bool = False
    is_held_for_prepaid_transition: bool = False


class _GrantList(BaseModel):
    data: list[_Grant]


class _CreditGrantsResponse(BaseModel):
    total_available: Decimal
    total_paid_available: Decimal
    grants: _GrantList

    def to_credits(self) -> OpenAIPlatformCredits:
        return OpenAIPlatformCredits(
            total_available_usd=self.total_available,
            total_paid_available_usd=self.total_paid_available,
            grants=[
                OpenAIPlatformCreditGrant(
                    granted_usd=grant.grant_amount,
                    used_usd=grant.used_amount,
                    effective_at=grant.effective_at,
                    expires_at=grant.expires_at,
                    is_scheduled=grant.is_scheduled,
                    is_held_for_prepaid_transition=grant.is_held_for_prepaid_transition,
                )
                for grant in self.grants.data
            ],
        )


class OpenAIPlatformProvider(Provider):
    name = "openai_platform"

    def __init__(self, settings: OpenAIPlatformSettings, client_factory: ProviderClientFactory) -> None:
        self.settings = settings
        self.client_factory = client_factory

    async def fetch(self) -> ProviderFetch:
        now = datetime.now(UTC)
        if not self.settings.url:
            return ProviderFetch(fetched_at=now, result=FetchError(error="OpenAI Platform proxy URL is not configured"))
        try:
            token = (
                self.settings.token_path.expanduser().read_text().strip()
                if self.settings.token_path
                else os.environ.get("AIQUOTA_OPENAI_PLATFORM_TOKEN", "")
            )
            if not token:
                raise ValueError("OpenAI Platform proxy token is not configured")
            url = self.settings.url.rstrip("/") + "/v1/dashboard/billing/credit_grants"
            headers = {"Authorization": f"Bearer {token}"}
            async with self.client_factory(self.name, {url}, 20.0) as client:
                response = await client.get(url, headers=headers)
            response.raise_for_status()
            credits = _CreditGrantsResponse.model_validate_json(response.content).to_credits()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            return ProviderFetch(fetched_at=now, result=FetchError.from_exception(exc, "OpenAI Platform credits"))
        return ProviderFetch(fetched_at=now, result=FetchSuccess(openai_platform_credits=credits))
