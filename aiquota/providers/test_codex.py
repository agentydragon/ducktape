import base64
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import pytest_bazel
import respx

from aiquota.models import (
    FetchError,
    FetchSuccess,
    HistoryKind,
    ResetCreditsObservation,
    TokenActivityDay,
    TokenActivityObservation,
)
from aiquota.providers.cli_proxy_api import (
    MANAGEMENT_API_CALL_PATH,
    MANAGEMENT_AUTH_FILES_PATH,
    CLIProxyAPIManagementClient,
)
from aiquota.providers.client import ProviderClientFactory, provider_client
from aiquota.providers.codex import (
    OAUTH_CLIENT_ID,
    PROFILE_URL,
    RESET_CREDITS_URL,
    TOKEN_URL,
    USAGE_URL,
    CodexProvider,
    CodexSettings,
)

if __name__ == "__main__":
    pytest_bazel.main()


def _jwt(exp: datetime) -> str:
    def enc(value: dict[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc({'exp': int(exp.timestamp())})}.sig"


def _auth(
    access_token: str,
    refresh_token: str = "refresh-token",
    account_id: str = "workspace-1",
    last_refresh: datetime | None = None,
) -> dict[str, Any]:
    return {
        "OPENAI_API_KEY": "stale-api-key",
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": "id-token",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "account_id": account_id,
        },
        "last_refresh": (last_refresh or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
    }


_USAGE_BODY = {
    "rate_limit": {"primary_window": {"used_percent": 12.5, "limit_window_seconds": 18000, "reset_after_seconds": 120}}
}


def _provider(path: Path) -> CodexProvider:
    return CodexProvider(CodexSettings(auth_path=path), provider_client())


def _management_provider() -> CodexProvider:
    return CodexProvider(
        CodexSettings(),
        provider_client(),
        CLIProxyAPIManagementClient("http://cliproxy.test/v0/management", "management-key", provider_client()),
    )


def _fixture(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((Path(__file__).parent / "fixtures" / name).read_text()))


async def test_refreshes_expired_access_token_before_usage(tmp_path: Path) -> None:
    expired = _jwt(datetime.now(UTC) - timedelta(minutes=5))
    fresh = _jwt(datetime.now(UTC) + timedelta(days=10))
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(_auth(expired, refresh_token="old-refresh")))
    seen_get_tokens: list[str] = []

    def usage_side_effect(request: httpx.Request) -> httpx.Response:
        seen_get_tokens.append(request.headers["Authorization"].removeprefix("Bearer "))
        return httpx.Response(200, json=_USAGE_BODY)

    with respx.mock(assert_all_called=False) as mock:
        post_route = mock.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={"access_token": fresh, "refresh_token": "new-refresh", "id_token": "new-id"}
            )
        )
        mock.get(USAGE_URL).mock(side_effect=usage_side_effect)
        output = await _provider(path).fetch()

    assert post_route.call_count == 1
    assert json.loads(post_route.calls.last.request.read()) == {
        "client_id": OAUTH_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": "old-refresh",
    }
    assert isinstance(output.result, FetchSuccess)
    assert seen_get_tokens == [fresh]
    saved = json.loads(path.read_text())
    assert saved["tokens"]["access_token"] == fresh
    assert saved["tokens"]["refresh_token"] == "new-refresh"
    assert saved["tokens"]["id_token"] == "new-id"


async def test_unauthorized_reloads_changed_auth_before_refreshing(tmp_path: Path) -> None:
    old = _jwt(datetime.now(UTC) + timedelta(days=10))
    fresh = _jwt(datetime.now(UTC) + timedelta(days=11))
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(_auth(old, refresh_token="old-refresh")))
    seen_get_tokens: list[str] = []

    def usage_side_effect(request: httpx.Request) -> httpx.Response:
        auth = request.headers["Authorization"].removeprefix("Bearer ")
        seen_get_tokens.append(auth)
        if len(seen_get_tokens) == 1:
            path.write_text(json.dumps(_auth(fresh, refresh_token="new-refresh")))
            return httpx.Response(401, text="unauthorized")
        return httpx.Response(200, json=_USAGE_BODY)

    with respx.mock(assert_all_called=False) as mock:
        post_route = mock.post(TOKEN_URL).mock(
            side_effect=AssertionError("token refresh should not be called after auth file changed")
        )
        mock.get(USAGE_URL).mock(side_effect=usage_side_effect)
        output = await _provider(path).fetch()

    assert isinstance(output.result, FetchSuccess)
    assert seen_get_tokens == [old, fresh]
    assert post_route.call_count == 0


async def test_refresh_failure_uses_token_written_by_another_process(tmp_path: Path) -> None:
    expired = _jwt(datetime.now(UTC) - timedelta(minutes=5))
    fresh = _jwt(datetime.now(UTC) + timedelta(days=10))
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(_auth(expired, refresh_token="old-refresh")))
    seen_get_tokens: list[str] = []

    def post_side_effect(request: httpx.Request) -> httpx.Response:
        path.write_text(json.dumps(_auth(fresh, refresh_token="new-refresh")))
        return httpx.Response(401, json={"error": {"code": "refresh_token_reused"}})

    def usage_side_effect(request: httpx.Request) -> httpx.Response:
        seen_get_tokens.append(request.headers["Authorization"].removeprefix("Bearer "))
        return httpx.Response(200, json=_USAGE_BODY)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).mock(side_effect=post_side_effect)
        mock.get(USAGE_URL).mock(side_effect=usage_side_effect)
        output = await _provider(path).fetch()

    assert isinstance(output.result, FetchSuccess)
    assert seen_get_tokens == [fresh]
    assert json.loads(path.read_text())["tokens"]["refresh_token"] == "new-refresh"


async def test_refresh_failure_without_new_auth_returns_fetch_error(tmp_path: Path) -> None:
    expired = _jwt(datetime.now(UTC) - timedelta(minutes=5))
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(_auth(expired, refresh_token="old-refresh")))

    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": {"code": "refresh_token_reused"}}))
        mock.get(USAGE_URL).mock(side_effect=AssertionError("usage should not be fetched after refresh failure"))
        output = await _provider(path).fetch()

    assert isinstance(output.result, FetchError)


async def test_usage_timeout_error_is_not_blank(tmp_path: Path) -> None:
    token = _jwt(datetime.now(UTC) + timedelta(days=10))
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(_auth(token)))

    def usage_timeout(request: httpx.Request) -> None:
        raise httpx.ReadTimeout("", request=request)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(USAGE_URL).mock(side_effect=usage_timeout)
        output = await _provider(path).fetch()

    assert isinstance(output.result, FetchError)
    assert output.result.error == "codex usage fetch: ReadTimeout"


async def test_weekly_primary_window_preserves_provider_duration(tmp_path: Path) -> None:
    token = _jwt(datetime.now(UTC) + timedelta(days=10))
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(_auth(token)))

    with respx.mock(assert_all_called=False) as mock:
        mock.get(USAGE_URL).mock(return_value=httpx.Response(200, json=_fixture("codex_weekly_primary.json")))
        output = await _provider(path).fetch()

    assert isinstance(output.result, FetchSuccess)
    assert len(output.result.windows) == 2
    assert output.result.windows[0].window_seconds == 7 * 86400
    assert output.result.windows[0].used_percent == 6
    assert output.result.windows[0].name is None
    assert output.result.windows[1].name == "GPT-5.3-Codex-Spark"
    assert not output.result.windows[1].display


async def test_usage_surfaces_authoritative_banked_reset_count(tmp_path: Path) -> None:
    token = _jwt(datetime.now(UTC) + timedelta(days=10))
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(_auth(token)))
    usage = {
        **_USAGE_BODY,
        # Codex may cap or omit detail rows. Never derive the count from them.
        "rate_limit_reset_credits": {"available_count": 3, "credits": []},
    }
    details = {
        "credits": [
            {
                "id": "expiring",
                "reset_type": "codexRateLimits",
                "status": "available",
                "granted_at": "2026-09-01T09:00:00Z",
                "expires_at": "2026-09-20T09:00:00Z",
            },
            {
                "id": "permanent",
                "reset_type": "codexRateLimits",
                "status": "available",
                "granted_at": "2026-09-01T09:00:00Z",
                "expires_at": None,
            },
        ]
    }

    with respx.mock(assert_all_called=False) as mock:
        mock.get(USAGE_URL).mock(return_value=httpx.Response(200, json=usage))
        mock.get(RESET_CREDITS_URL).mock(return_value=httpx.Response(200, json=details))
        output = await _provider(path).fetch()

    assert isinstance(output.result, FetchSuccess)
    assert output.result.available_reset_credits == 3
    assert output.result.available_reset_credit_expiries == [datetime(2026, 9, 20, 9, 0, tzinfo=UTC)]


async def test_credit_detail_failure_keeps_the_authoritative_banked_count(tmp_path: Path) -> None:
    token = _jwt(datetime.now(UTC) + timedelta(days=10))
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(_auth(token)))
    usage = {**_USAGE_BODY, "rate_limit_reset_credits": {"available_count": 1}}

    with respx.mock(assert_all_called=False) as mock:
        mock.get(USAGE_URL).mock(return_value=httpx.Response(200, json=usage))
        mock.get(RESET_CREDITS_URL).mock(return_value=httpx.Response(503, text="temporarily unavailable"))
        output = await _provider(path).fetch()

    assert isinstance(output.result, FetchSuccess)
    assert output.result.available_reset_credits == 1
    assert output.result.available_reset_credit_expiries == []


async def test_management_api_uses_runtime_codex_auth_index() -> None:
    with respx.mock(assert_all_called=False) as mock:
        auth_files = mock.get("http://cliproxy.test/v0/management" + MANAGEMENT_AUTH_FILES_PATH).mock(
            return_value=httpx.Response(
                200, json={"files": [{"provider": "codex", "auth_index": "codex-auth", "disabled": False}]}
            )
        )
        api_call = mock.post("http://cliproxy.test/v0/management" + MANAGEMENT_API_CALL_PATH).mock(
            return_value=httpx.Response(
                200,
                json={
                    "status_code": 200,
                    "header": {"Content-Type": ["application/json"]},
                    "body": json.dumps(_USAGE_BODY),
                },
            )
        )

        output = await _management_provider().fetch()

    assert isinstance(output.result, FetchSuccess)
    assert auth_files.calls.last.request.headers["Authorization"] == "Bearer management-key"
    assert api_call.calls.last.request.headers["Authorization"] == "Bearer management-key"
    request_body = json.loads(api_call.calls.last.request.read())
    assert request_body == {
        "auth_index": "codex-auth",
        "method": "GET",
        "url": USAGE_URL,
        "header": {"Authorization": "Bearer $TOKEN$", "User-Agent": "codex_cli_rs/0.125.0 (Linux; x86_64)"},
    }


async def test_management_api_surfaces_known_banked_reset_expiries() -> None:
    usage = {**_USAGE_BODY, "rate_limit_reset_credits": {"available_count": 1}}
    details = {
        "credits": [
            {
                "id": "expiring",
                "reset_type": "codexRateLimits",
                "status": "available",
                "granted_at": "2026-09-01T09:00:00Z",
                "expires_at": "2026-09-20T09:00:00Z",
            }
        ]
    }
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://cliproxy.test/v0/management" + MANAGEMENT_AUTH_FILES_PATH).mock(
            return_value=httpx.Response(
                200, json={"files": [{"provider": "codex", "auth_index": "codex-auth", "disabled": False}]}
            )
        )

        def api_call(request: httpx.Request) -> httpx.Response:
            url = json.loads(request.read())["url"]
            body = usage if url == USAGE_URL else details
            return httpx.Response(200, json={"status_code": 200, "header": {}, "body": json.dumps(body)})

        mock.post("http://cliproxy.test/v0/management" + MANAGEMENT_API_CALL_PATH).mock(side_effect=api_call)
        output = await _management_provider().fetch()

    assert isinstance(output.result, FetchSuccess)
    assert output.result.available_reset_credits == 1
    assert output.result.available_reset_credit_expiries == [datetime(2026, 9, 20, 9, 0, tzinfo=UTC)]


async def test_management_api_rejects_multiple_codex_auth_files() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://cliproxy.test/v0/management" + MANAGEMENT_AUTH_FILES_PATH).mock(
            return_value=httpx.Response(
                200,
                json={
                    "files": [
                        {"provider": "codex", "auth_index": "codex-a"},
                        {"provider": "codex", "auth_index": "codex-b"},
                    ]
                },
            )
        )
        api_call = mock.post("http://cliproxy.test/v0/management" + MANAGEMENT_API_CALL_PATH).mock(
            side_effect=AssertionError("must not choose an ambiguous auth file")
        )

        output = await _management_provider().fetch()

    assert isinstance(output.result, FetchError)
    assert output.result.error == "CLIProxyAPI integration: expected exactly one available Codex auth file"
    assert api_call.call_count == 0


async def test_history_reads_daily_token_buckets_and_reset_credits(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(_auth(_jwt(datetime.now(UTC) + timedelta(days=10)))))

    with respx.mock(assert_all_called=False) as mock:
        mock.get(PROFILE_URL).mock(return_value=httpx.Response(200, json=_fixture("codex_profile.json")))
        mock.get(RESET_CREDITS_URL).mock(return_value=httpx.Response(200, json=_fixture("codex_reset_credits.json")))
        observations = await _provider(path).fetch_history()

    activity, credits = observations
    assert isinstance(activity.payload, TokenActivityObservation)
    assert activity.payload.days[0] == TokenActivityDay(start_date=date(2026, 8, 25), tokens=12345678)
    assert [day.tokens for day in activity.payload.days] == [12345678, 0, 9876543]
    assert isinstance(credits.payload, ResetCreditsObservation)
    assert [(credit.credit_id, credit.status) for credit in credits.payload.credits] == [
        ("credit_a", "consumed"),
        ("credit_b", "available"),
    ]
    assert credits.payload.credits[0].expires_at == datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
    assert credits.payload.credits[1].expires_at is None


async def test_history_skips_an_endpoint_the_account_cannot_see(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(_auth(_jwt(datetime.now(UTC) + timedelta(days=10)))))

    with respx.mock(assert_all_called=False) as mock:
        mock.get(PROFILE_URL).mock(return_value=httpx.Response(200, json=_fixture("codex_profile.json")))
        mock.get(RESET_CREDITS_URL).mock(return_value=httpx.Response(404, text="not found"))
        observations = await _provider(path).fetch_history()

    assert [observation.payload.kind for observation in observations] == [HistoryKind.TOKEN_ACTIVITY]


async def test_history_propagates_an_upstream_failure(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(_auth(_jwt(datetime.now(UTC) + timedelta(days=10)))))

    with respx.mock(assert_all_called=False) as mock:
        mock.get(PROFILE_URL).mock(return_value=httpx.Response(500, text="boom"))
        with pytest.raises(httpx.HTTPStatusError):
            await _provider(path).fetch_history()


async def test_history_captures_each_endpoint_under_its_own_key(tmp_path: Path) -> None:
    """Two endpoints in one cycle must not overwrite each other's raw body."""

    path = tmp_path / "auth.json"
    path.write_text(json.dumps(_auth(_jwt(datetime.now(UTC) + timedelta(days=10)))))
    capture_keys: list[str] = []

    def factory(capture_key: str, response_urls: set[str], timeout: float) -> httpx.AsyncClient:
        capture_keys.append(capture_key)
        return httpx.AsyncClient(timeout=timeout)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(PROFILE_URL).mock(return_value=httpx.Response(200, json=_fixture("codex_profile.json")))
        mock.get(RESET_CREDITS_URL).mock(return_value=httpx.Response(200, json=_fixture("codex_reset_credits.json")))
        await CodexProvider(CodexSettings(auth_path=path), cast(ProviderClientFactory, factory)).fetch_history()

    assert capture_keys == ["codex_token_activity", "codex_reset_credits"]


async def test_management_history_selects_codex_auth_per_endpoint() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://cliproxy.test/v0/management" + MANAGEMENT_AUTH_FILES_PATH).mock(
            return_value=httpx.Response(
                200, json={"files": [{"provider": "codex", "auth_index": "codex-auth", "disabled": False}]}
            )
        )
        bodies = {
            PROFILE_URL: json.dumps(_fixture("codex_profile.json")),
            RESET_CREDITS_URL: json.dumps(_fixture("codex_reset_credits.json")),
        }

        def api_call(request: httpx.Request) -> httpx.Response:
            requested = json.loads(request.read())["url"]
            return httpx.Response(
                200,
                json={"status_code": 200, "header": {"Content-Type": ["application/json"]}, "body": bodies[requested]},
            )

        mock.post("http://cliproxy.test/v0/management" + MANAGEMENT_API_CALL_PATH).mock(side_effect=api_call)
        observations = await _management_provider().fetch_history()

    assert [observation.payload.kind for observation in observations] == [
        HistoryKind.TOKEN_ACTIVITY,
        HistoryKind.RESET_CREDITS,
    ]
