from datetime import UTC, datetime

import httpx
import pytest_bazel

from aiquota.models import FetchError, FetchSuccess
from aiquota.providers.anthropic_platform import AnthropicPlatformProvider, AnthropicPlatformSettings
from aiquota.providers.client import provider_client


def _credits_response() -> dict[str, object]:
    tranche = {
        "id": "grant-example",
        "name": None,
        "currency": "USD",
        "granted_at": "2026-01-01T00:00:00Z",
        "expires_at": "2027-01-01T00:00:00Z",
        "granted": {"credits": {"amount_minor": 10000, "exponent": 2}, "money": None},
        "remaining": {"credits": {"amount_minor": 7500, "exponent": 2}, "money": None},
        "granted_amount_minor_units": 10000,
        "remaining_amount_minor_units": 7500,
        "program_id": "program-example",
        "scope": {"kind": "unscoped", "applies_to": []},
    }
    promo = {
        **tranche,
        "id": "promo-example",
        "scope": {
            "kind": "scoped",
            "applies_to": [
                {"product": "claude_code", "model_family": None, "model_version": None},
                {"product": "claude_api", "model_family": "sonnet", "model_version": None},
            ],
        },
    }
    return {
        "amount": 12500,
        "amount_without_scoped_credits": 10000,
        "currency": "USD",
        "balance": {"credits": {"amount_minor": 12500, "exponent": 2}, "money": None},
        "tranches": [tranche],
        "promo_tranches": [promo],
    }


async def test_fetch_enumerates_api_organizations_and_keeps_scopes_distinct(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("read-token")
    requests: list[httpx.Request] = []

    def response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer read-token"
        if request.url.path == "/proxy/anthropic/api/organizations":
            return httpx.Response(
                200,
                json=[
                    {"uuid": "api-org-1", "name": "Main org", "capabilities": ["api"]},
                    {"uuid": "consumer-org", "name": "Consumer org", "capabilities": ["claude"]},
                    {"uuid": "api-org-2", "name": "Other API org", "capabilities": ["customer_terms:standard", "api"]},
                ],
            )
        if request.url.path == "/proxy/anthropic/api/organizations/api-org-1/prepaid/credits":
            return httpx.Response(200, json=_credits_response())
        if request.url.path == "/proxy/anthropic/api/organizations/api-org-2/prepaid/credits":
            return httpx.Response(403, json={"error": "private upstream message"})
        raise AssertionError(f"unexpected URL path: {request.url.path}")

    provider = AnthropicPlatformProvider(
        AnthropicPlatformSettings(enabled=True, url="https://proxy.example/proxy/anthropic", token_path=token_path),
        provider_client(transport=httpx.MockTransport(response)),
    )
    result = (await provider.fetch()).result

    assert isinstance(result, FetchSuccess)
    assert result.anthropic_platform_credits is not None
    organizations = result.anthropic_platform_credits.organizations
    assert [organization.name for organization in organizations] == ["Main org", "Other API org"]
    assert organizations[0].balance is not None
    assert organizations[0].balance.credits is not None
    assert organizations[0].balance.credits.amount_minor == 12500
    assert len(organizations[0].tranches) == 1
    promo = organizations[0].promo_tranches[0]
    assert promo.expires_at == datetime(2027, 1, 1, tzinfo=UTC)
    assert promo.scope.kind == "scoped"
    assert [item.product for item in promo.scope.applies_to] == ["claude_code", "claude_api"]
    assert organizations[1].balance is None
    assert organizations[1].error == "upstream rejected request (HTTP 403)"
    assert {request.url.path for request in requests} == {
        "/proxy/anthropic/api/organizations",
        "/proxy/anthropic/api/organizations/api-org-1/prepaid/credits",
        "/proxy/anthropic/api/organizations/api-org-2/prepaid/credits",
    }


async def test_organization_list_failure_is_a_provider_error(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("read-token")
    provider = AnthropicPlatformProvider(
        AnthropicPlatformSettings(enabled=True, url="https://proxy.example/proxy/anthropic", token_path=token_path),
        provider_client(transport=httpx.MockTransport(lambda request: httpx.Response(502))),
    )

    result = (await provider.fetch()).result

    assert isinstance(result, FetchError)
    assert "Anthropic Platform orgs" in result.error


if __name__ == "__main__":
    pytest_bazel.main()
