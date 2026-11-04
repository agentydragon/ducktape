from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest_bazel

from aiquota.models import FetchError, FetchSuccess
from aiquota.providers.client import provider_client
from aiquota.providers.openai_platform import OpenAIPlatformProvider, OpenAIPlatformSettings


async def test_fetch_reports_balance_and_grant_expiry(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("read-token")

    def response(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/proxy/openai/v1/dashboard/billing/credit_grants"
        assert request.headers["authorization"] == "Bearer read-token"
        assert "openai-organization" not in request.headers
        return httpx.Response(
            200,
            json={
                "total_available": 25.75,
                "total_paid_available": 10,
                "grants": {
                    "data": [
                        {
                            "grant_amount": 30,
                            "used_amount": 4.25,
                            "effective_at": 1700000000,
                            "expires_at": 1800000000,
                            "is_scheduled": False,
                            "is_held_for_prepaid_transition": False,
                        },
                        {
                            "grant_amount": 10,
                            "used_amount": 10,
                            "effective_at": 1700000000,
                            "expires_at": None,
                            "is_scheduled": False,
                            "is_held_for_prepaid_transition": False,
                        },
                    ]
                },
            },
        )

    provider = OpenAIPlatformProvider(
        OpenAIPlatformSettings(enabled=True, url="https://proxy.example/proxy/openai", token_path=token_path),
        provider_client(transport=httpx.MockTransport(response)),
    )
    result = (await provider.fetch()).result
    assert isinstance(result, FetchSuccess)
    assert result.openai_platform_credits is not None
    assert result.openai_platform_credits.total_available_usd == Decimal("25.75")
    assert result.openai_platform_credits.grants[0].expires_at == datetime.fromtimestamp(1800000000, UTC)
    assert result.openai_platform_credits.grants[1].expires_at is None


async def test_failed_fetch_does_not_report_zero(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("read-token")
    provider = OpenAIPlatformProvider(
        OpenAIPlatformSettings(enabled=True, url="https://proxy.example/proxy/openai", token_path=token_path),
        provider_client(transport=httpx.MockTransport(lambda request: httpx.Response(503))),
    )
    assert isinstance((await provider.fetch()).result, FetchError)


if __name__ == "__main__":
    pytest_bazel.main()
