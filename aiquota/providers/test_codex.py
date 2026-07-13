import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest_bazel
import respx

from aiquota.models import FetchError, FetchSuccess
from aiquota.providers.codex import OAUTH_CLIENT_ID, TOKEN_URL, USAGE_URL, CodexProvider, CodexSettings

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
        output = await CodexProvider(CodexSettings(auth_path=path)).fetch()

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
        output = await CodexProvider(CodexSettings(auth_path=path)).fetch()

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
        output = await CodexProvider(CodexSettings(auth_path=path)).fetch()

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
        output = await CodexProvider(CodexSettings(auth_path=path)).fetch()

    assert isinstance(output.result, FetchError)


async def test_usage_timeout_error_is_not_blank(tmp_path: Path) -> None:
    token = _jwt(datetime.now(UTC) + timedelta(days=10))
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(_auth(token)))

    def usage_timeout(request: httpx.Request) -> None:
        raise httpx.ReadTimeout("", request=request)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(USAGE_URL).mock(side_effect=usage_timeout)
        output = await CodexProvider(CodexSettings(auth_path=path)).fetch()

    assert isinstance(output.result, FetchError)
    assert output.result.error == "codex usage fetch: ReadTimeout"


async def test_weekly_primary_window_preserves_provider_duration(tmp_path: Path) -> None:
    token = _jwt(datetime.now(UTC) + timedelta(days=10))
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(_auth(token)))

    with respx.mock(assert_all_called=False) as mock:
        mock.get(USAGE_URL).mock(return_value=httpx.Response(200, json=_fixture("codex_weekly_primary.json")))
        output = await CodexProvider(CodexSettings(auth_path=path)).fetch()

    assert isinstance(output.result, FetchSuccess)
    assert len(output.result.windows) == 2
    assert output.result.windows[0].window_seconds == 7 * 86400
    assert output.result.windows[0].used_percent == 6
    assert output.result.windows[0].name is None
    assert output.result.windows[1].name == "GPT-5.3-Codex-Spark"
    assert not output.result.windows[1].display
