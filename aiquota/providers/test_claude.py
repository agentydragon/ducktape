import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import pytest_bazel
import respx
from pydantic import SecretStr

from aiquota.models import ClaudeResetGrant, FetchSuccess, PrepaidBalanceAvailable, PrepaidBalanceUnavailable
from aiquota.providers.claude import (
    PLAIN_USAGE_URL,
    PROFILE_URL,
    USAGE_URL,
    ClaudeProvider,
    ClaudeSettings,
    _prepaid_balance_from_body,
    _spend_to_extra_spend,
    _to_success,
)
from aiquota.providers.cli_proxy_api import (
    MANAGEMENT_API_CALL_PATH,
    MANAGEMENT_AUTH_FILES_PATH,
    CLIProxyAPIManagementClient,
)
from aiquota.providers.client import provider_client
from devinfra.claude.claude_api.oauth_client import TOKEN_URL
from devinfra.claude.claude_api.usage import UsageResponse

if __name__ == "__main__":
    pytest_bazel.main()


@pytest.mark.parametrize("enabled", [True, False, None])
def test_spend_shape_retains_monthly_totals_independently_of_enabled(enabled: bool | None) -> None:
    usage = UsageResponse.model_validate(
        {
            "spend": {
                "enabled": enabled,
                "limit": {"amount_minor": 250000, "currency": "USD", "exponent": 2},
                "used": {"amount_minor": 12345, "currency": "USD", "exponent": 2},
                "percent": 4.94,
                "severity": "normal",
            }
        }
    )
    extra = _to_success(usage).extra_spend
    assert extra is not None
    assert extra.is_enabled is (enabled is True)
    assert extra.monthly_limit_usd == 2500.0
    assert extra.used_usd == 123.45
    assert extra.utilization == 4.94


@pytest.mark.parametrize(
    "spend",
    [
        None,
        {"enabled": False, "disabled_reason": "payment_method_required"},
        {"enabled": True, "used": {"amount_minor": 12345}},
        {"enabled": False, "limit": {"amount_minor": 250000}},
    ],
)
def test_spend_without_complete_totals_remains_unknown(spend: dict[str, object] | None) -> None:
    usage = UsageResponse.model_validate({"spend": spend})
    assert _spend_to_extra_spend(usage.spend) is None


def test_raw_usage_fixture_preserves_provider_windows() -> None:
    usage = UsageResponse.model_validate_json((Path(__file__).parent / "fixtures" / "claude_usage.json").read_text())

    result = _to_success(usage)

    assert [(window.window_seconds, window.used_percent) for window in result.windows] == [
        (5 * 3600, 73),
        (7 * 86400, 75),
    ]


def test_observed_reset_inventory_preserves_scope_and_unknown() -> None:
    # Shape observed from Claude's OAuth usage endpoint, with account IDs replaced.
    usage = UsageResponse.model_validate_json(
        (Path(__file__).parent / "fixtures" / "claude_reset_grants.json").read_text()
    )
    result = _to_success(usage)
    assert result.claude_reset_grants == [
        ClaudeResetGrant(
            label="Fable resets",
            remaining=0,
            original=1,
            starts_at=datetime(2026, 11, 9, 18, tzinfo=UTC),
            expires_at=datetime(2027, 3, 9, 7, 59, tzinfo=UTC),
            usable_now=False,
        ),
        ClaudeResetGrant(
            label="Fable resets",
            remaining=2,
            original=2,
            starts_at=datetime(2026, 11, 9, 18, tzinfo=UTC),
            expires_at=datetime(2027, 3, 9, 7, 59, tzinfo=UTC),
            usable_now=True,
        ),
    ]
    for block in (None, {"eligible": False, "grants": []}, {"eligible": True, "grants": [{"resets_left": 1}]}):
        result = _to_success(UsageResponse.model_validate({"five_hour": {"utilization": 10}, "cedar_ember": block}))
        assert result.claude_reset_grants is None
        assert result.windows[0].used_percent == 10
    assert (
        _to_success(UsageResponse.model_validate({"cedar_ember": {"eligible": True, "grants": []}})).claude_reset_grants
        == []
    )


def test_prepaid_credits_uses_exact_nested_minor_units_not_rounded_or_tranche_values() -> None:
    response = Path(__file__).parent / "fixtures" / "claude_prepaid_credits.json"

    balance = _prepaid_balance_from_body(response.read_text())

    assert balance == PrepaidBalanceAvailable(amount_minor=12345, exponent=2, currency="USD")


async def test_explicit_token_is_read_only_and_never_refreshed(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    path.write_text('{"claudeAiOauth":{"accessToken":"expired", "refreshToken":"refresh", "expiresAt":0}}')
    provider = ClaudeProvider(
        ClaudeSettings(credentials_path=path, access_token=SecretStr("placeholder")), provider_client()
    )

    with respx.mock(assert_all_called=False) as mock:
        post_route = mock.post(TOKEN_URL).mock(side_effect=AssertionError("read-only provider must not refresh"))
        usage_route = mock.get(USAGE_URL).mock(
            return_value=httpx.Response(
                200, json={"five_hour": {"utilization": 10, "resets_at": "2026-08-20T05:00:00Z"}}
            )
        )
        profile_route = mock.get(PROFILE_URL).mock(
            return_value=httpx.Response(200, json={"organization": {"uuid": "123e4567-e89b-12d3-a456-426614174000"}})
        )
        balance_url = (
            "https://api.anthropic.com/api/oauth/organizations/123e4567-e89b-12d3-a456-426614174000/prepaid/credits"
        )
        balance_route = mock.get(balance_url).mock(
            return_value=httpx.Response(
                200,
                content=(Path(__file__).parent / "fixtures" / "claude_prepaid_credits.json").read_bytes(),
                headers={"Content-Type": "application/json"},
            )
        )
        output = await provider.fetch()

    assert isinstance(output.result, FetchSuccess)
    assert output.result.prepaid_balance == PrepaidBalanceAvailable(amount_minor=12345, exponent=2, currency="USD")
    assert post_route.call_count == 0
    assert usage_route.calls.last.request.headers["Authorization"] == "Bearer placeholder"
    assert usage_route.calls.last.request.headers["User-Agent"] == "claude-cli/2.1.292 (external, cli) aiquota/1.0"
    assert profile_route.calls.last.request.headers["Authorization"] == "Bearer placeholder"
    assert balance_route.calls.last.request.headers["Authorization"] == "Bearer placeholder"
    assert balance_route.calls.last.request.headers["anthropic-beta"] == "oauth-2025-04-20"
    assert "placeholder" not in path.read_text()


async def test_management_api_uses_claude_auth_index_and_does_not_use_legacy_token() -> None:
    provider = ClaudeProvider(
        ClaudeSettings(access_token=SecretStr("legacy-placeholder")),
        provider_client(),
        CLIProxyAPIManagementClient("http://cliproxy.test/v0/management", "management-key", provider_client()),
    )
    usage_body = {"five_hour": {"utilization": 10, "resets_at": "2026-08-20T05:00:00Z"}}
    profile_body = {"organization": {"uuid": "123e4567-e89b-12d3-a456-426614174000"}}
    balance_url = (
        "https://api.anthropic.com/api/oauth/organizations/123e4567-e89b-12d3-a456-426614174000/prepaid/credits"
    )
    balance_body = json.loads((Path(__file__).parent / "fixtures" / "claude_prepaid_credits.json").read_text())

    with respx.mock(assert_all_called=False) as mock:
        auth_files = mock.get("http://cliproxy.test/v0/management" + MANAGEMENT_AUTH_FILES_PATH).mock(
            return_value=httpx.Response(200, json={"files": [{"provider": "claude", "auth_index": "claude-auth"}]})
        )

        def api_call_response(request: httpx.Request) -> httpx.Response:
            endpoint = json.loads(request.content)["url"]
            bodies = {USAGE_URL: usage_body, PROFILE_URL: profile_body, balance_url: balance_body}
            return httpx.Response(
                200,
                json={
                    "status_code": 200,
                    "header": {"Content-Type": ["application/json"]},
                    "body": json.dumps(bodies[endpoint]),
                },
                request=request,
            )

        api_call = mock.post("http://cliproxy.test/v0/management" + MANAGEMENT_API_CALL_PATH).mock(
            side_effect=api_call_response
        )
        legacy_usage = mock.get(USAGE_URL).mock(side_effect=AssertionError("legacy token path must not be used"))
        output = await provider.fetch()

    assert isinstance(output.result, FetchSuccess)
    assert output.result.prepaid_balance == PrepaidBalanceAvailable(amount_minor=12345, exponent=2, currency="USD")
    assert len(auth_files.calls) == 3
    assert all(call.request.headers["Authorization"] == "Bearer management-key" for call in api_call.calls)
    payloads = [json.loads(call.request.content) for call in api_call.calls]
    usage_request = next(payload for payload in payloads if payload["url"] == USAGE_URL)
    profile_request = next(payload for payload in payloads if payload["url"] == PROFILE_URL)
    prepaid_request = next(payload for payload in payloads if payload["url"] == balance_url)
    assert usage_request == {
        "auth_index": "claude-auth",
        "method": "GET",
        "url": USAGE_URL,
        "header": {
            "Authorization": "Bearer $TOKEN$",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-cli/2.1.292 (external, cli) aiquota/1.0",
        },
    }
    assert profile_request["header"] == {"Authorization": "Bearer $TOKEN$", "anthropic-beta": "oauth-2025-04-20"}
    assert prepaid_request["header"] == profile_request["header"]
    assert legacy_usage.call_count == 0


async def test_rejected_reset_option_falls_back_to_plain_usage() -> None:
    provider = ClaudeProvider(ClaudeSettings(access_token=SecretStr("placeholder")), provider_client())
    with respx.mock(assert_all_called=False) as mock:
        mock.get(USAGE_URL).mock(return_value=httpx.Response(400))
        plain = mock.get(PLAIN_USAGE_URL).mock(
            return_value=httpx.Response(200, json={"seven_day": {"utilization": 42}})
        )
        mock.get(PROFILE_URL).mock(return_value=httpx.Response(503))
        output = await provider.fetch()
    assert isinstance(output.result, FetchSuccess)
    assert output.result.windows[0].used_percent == 42
    assert output.result.claude_reset_grants is None
    assert plain.called


async def test_management_rejected_reset_option_falls_back_to_plain_usage() -> None:
    management = CLIProxyAPIManagementClient("http://cliproxy.test/v0/management", "management-key", provider_client())
    provider = ClaudeProvider(ClaudeSettings(), provider_client(), management_client=management)
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://cliproxy.test/v0/management" + MANAGEMENT_AUTH_FILES_PATH).mock(
            return_value=httpx.Response(200, json={"files": [{"provider": "claude", "auth_index": "claude-auth"}]})
        )

        def response(request: httpx.Request) -> httpx.Response:
            url = json.loads(request.content)["url"]
            status = 200 if url == PLAIN_USAGE_URL else 400
            body = json.dumps({"seven_day": {"utilization": 42}}) if status == 200 else "unsupported"
            return httpx.Response(200, json={"status_code": status, "body": body}, request=request)

        calls = mock.post("http://cliproxy.test/v0/management" + MANAGEMENT_API_CALL_PATH).mock(side_effect=response)
        output = await provider.fetch()
    assert isinstance(output.result, FetchSuccess)
    assert output.result.windows[0].used_percent == 42
    assert output.result.claude_reset_grants is None
    assert [json.loads(call.request.content)["url"] for call in calls.calls][:2] == [USAGE_URL, PLAIN_USAGE_URL]


async def test_prepaid_lookup_failure_keeps_successful_usage_and_marks_balance_unavailable() -> None:
    provider = ClaudeProvider(ClaudeSettings(access_token=SecretStr("placeholder")), provider_client())

    with respx.mock(assert_all_called=False) as mock:
        mock.get(USAGE_URL).mock(return_value=httpx.Response(200, json={"five_hour": {"utilization": 10}}))
        mock.get(PROFILE_URL).mock(return_value=httpx.Response(503, text="supplemental endpoint unavailable"))
        output = await provider.fetch()

    assert isinstance(output.result, FetchSuccess)
    assert output.result.windows[0].used_percent == 10
    assert isinstance(output.result.prepaid_balance, PrepaidBalanceUnavailable)
