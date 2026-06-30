"""Tests for airlock.oauth.refresh."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_bazel

from airlock.oauth.provider import (
    ACCESS_TOKEN_FIELDS,
    GenericOAuth2Provider,
    OAuth2ProviderConfig,
    TokenData,
    TokenSecretConfig,
)
from airlock.oauth.refresh import check_scope_drift, token_refresh_loop


@pytest.fixture
def provider() -> GenericOAuth2Provider:
    return GenericOAuth2Provider(
        OAuth2ProviderConfig(
            name="test",
            display_name="Test Provider",
            authorize_url="https://example.com/authorize",
            token_url="https://example.com/token",
            scopes=["daily"],
            redirect_uri="https://example.com/callback/test",
            refresh_secret=TokenSecretConfig(name="test-tokens"),
            access_secret=TokenSecretConfig(name="test-access-token"),
            refresh_margin_seconds=3600,
        ),
        client_id="test-id",
        client_secret="test-secret",
        default_redirect_uri="https://example.com/oauth/callback",
    )


def _make_token(*, hours_until_expiry: float) -> TokenData:
    return TokenData(
        access_token="old-access",
        refresh_token="old-refresh",
        expires_at=datetime.now(UTC) + timedelta(hours=hours_until_expiry),
        scope="daily",
    )


def _make_refreshed_token() -> TokenData:
    return TokenData(
        access_token="new-access",
        refresh_token="new-refresh",
        expires_at=datetime.now(UTC) + timedelta(days=30),
        scope="daily",
    )


async def _run_loop_briefly(
    providers: dict[str, GenericOAuth2Provider], k8s_store: AsyncMock, namespace: str, sleep: float = 0.05
) -> None:
    task = asyncio.create_task(token_refresh_loop(providers, k8s_store, namespace, check_interval=0))
    await asyncio.sleep(sleep)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_refresh_loop_refreshes_expiring_token(provider: GenericOAuth2Provider) -> None:
    expiring_token = _make_token(hours_until_expiry=0.5)
    refreshed_token = _make_refreshed_token()

    mock_store = AsyncMock()
    mock_store.read_token.return_value = expiring_token

    with patch.object(provider, "refresh_tokens", return_value=refreshed_token):
        await _run_loop_briefly({"test": provider}, mock_store, "test-ns")

    mock_store.read_token.assert_called_with("test-tokens", "test-ns")
    mock_store.write_token.assert_any_call("test-tokens", "test-ns", refreshed_token)
    mock_store.write_token.assert_any_call("test-access-token", "test-ns", refreshed_token, fields=ACCESS_TOKEN_FIELDS)


async def test_refresh_loop_skips_fresh_token(provider: GenericOAuth2Provider) -> None:
    fresh_token = _make_token(hours_until_expiry=720)

    mock_store = AsyncMock()
    mock_store.read_token.return_value = fresh_token

    with patch.object(provider, "refresh_tokens") as mock_refresh:
        await _run_loop_briefly({"test": provider}, mock_store, "test-ns")

    mock_refresh.assert_not_called()
    mock_store.write_token.assert_not_called()


async def test_refresh_loop_skips_unconnected_provider(provider: GenericOAuth2Provider) -> None:
    mock_store = AsyncMock()
    mock_store.read_token.return_value = None

    with patch.object(provider, "refresh_tokens") as mock_refresh:
        await _run_loop_briefly({"test": provider}, mock_store, "test-ns")

    mock_refresh.assert_not_called()


async def test_refresh_loop_continues_on_error(provider: GenericOAuth2Provider) -> None:
    expiring_token = _make_token(hours_until_expiry=0.5)

    mock_store = AsyncMock()
    mock_store.read_token.return_value = expiring_token

    call_count = 0

    async def failing_refresh(refresh_token: str) -> TokenData:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("network error")

    with patch.object(provider, "refresh_tokens", side_effect=failing_refresh):
        await _run_loop_briefly({"test": provider}, mock_store, "test-ns", sleep=0.1)

    assert call_count >= 2
    mock_store.write_token.assert_not_called()


async def test_refresh_loop_keeps_access_secret_when_refresh_fails_for_valid_token(
    provider: GenericOAuth2Provider, caplog: pytest.LogCaptureFixture
) -> None:
    expiring_token = _make_token(hours_until_expiry=0.5)

    mock_store = AsyncMock()
    mock_store.read_token.return_value = expiring_token

    with (
        caplog.at_level("WARNING"),
        patch.object(provider, "refresh_tokens", side_effect=RuntimeError("network error")),
    ):
        await _run_loop_briefly({"test": provider}, mock_store, "test-ns")

    mock_store.delete_secret.assert_not_called()
    assert any(
        "Leaving access secret for test unchanged after refresh failure because the token remains valid until"
        in record.message
        for record in caplog.records
    )


async def test_refresh_loop_deletes_access_secret_when_refresh_fails_for_expired_token(
    provider: GenericOAuth2Provider, caplog: pytest.LogCaptureFixture
) -> None:
    expired_token = _make_token(hours_until_expiry=-0.5)

    mock_store = AsyncMock()
    mock_store.read_token.return_value = expired_token

    with (
        caplog.at_level("WARNING"),
        patch.object(provider, "refresh_tokens", side_effect=RuntimeError("network error")),
    ):
        await _run_loop_briefly({"test": provider}, mock_store, "test-ns")

    mock_store.delete_secret.assert_any_call("test-access-token", "test-ns")
    assert any(
        "Deleting access secret for test because refresh failed and the token expired at" in record.message
        for record in caplog.records
    )


async def test_refresh_loop_logs_when_refresh_token_cannot_be_read(
    provider: GenericOAuth2Provider, caplog: pytest.LogCaptureFixture
) -> None:
    mock_store = AsyncMock()
    mock_store.read_token.side_effect = RuntimeError("k8s down")

    with caplog.at_level("WARNING"), patch.object(provider, "refresh_tokens") as mock_refresh:
        await _run_loop_briefly({"test": provider}, mock_store, "test-ns")

    mock_refresh.assert_not_called()
    mock_store.delete_secret.assert_not_called()
    assert any(
        "Leaving access secret for test unchanged after refresh failure because the refresh token could not be read."
        in record.message
        for record in caplog.records
    )


async def test_refresh_loop_deletes_orphaned_secrets(provider: GenericOAuth2Provider) -> None:
    # delete_orphaned_secrets should be called with the set of names declared in config.
    mock_store = AsyncMock()
    mock_store.read_token.return_value = None

    await _run_loop_briefly({"test": provider}, mock_store, "test-ns")

    mock_store.delete_orphaned_secrets.assert_called_with("test-ns", frozenset({"test-tokens", "test-access-token"}))


def test_scope_drift_no_drift_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    warned: set[tuple[str, str]] = set()
    with caplog.at_level("WARNING"):
        check_scope_drift("bsc", ["openid", "interop"], "interop openid", warned)
    assert caplog.records == []
    assert warned == set()


def test_scope_drift_warns_once_per_granted(caplog: pytest.LogCaptureFixture) -> None:
    warned: set[tuple[str, str]] = set()
    with caplog.at_level("WARNING"):
        check_scope_drift("bsc", ["openid", "interop", "PatientEOB"], "interop openid", warned)
        check_scope_drift("bsc", ["openid", "interop", "PatientEOB"], "interop openid", warned)
        check_scope_drift("bsc", ["openid", "interop", "PatientEOB"], "interop openid", warned)
    assert len(caplog.records) == 1
    msg = caplog.records[0].message
    assert "bsc" in msg
    assert "PatientEOB" in msg  # missing
    assert "Re-authorize at /oauth/authorize/bsc" in msg


def test_scope_drift_re_warns_when_granted_changes(caplog: pytest.LogCaptureFixture) -> None:
    warned: set[tuple[str, str]] = set()
    with caplog.at_level("WARNING"):
        check_scope_drift("bsc", ["openid", "interop"], "interop", warned)
        check_scope_drift("bsc", ["openid", "interop"], "openid", warned)  # different grant string
    assert len(caplog.records) == 2


async def test_refresh_loop_warns_on_scope_drift(
    provider: GenericOAuth2Provider, caplog: pytest.LogCaptureFixture
) -> None:
    # Token granted just "daily" but config requests two scopes — config has drifted.
    drifted_token = TokenData(
        access_token="a",
        refresh_token="r",
        expires_at=datetime.now(UTC) + timedelta(days=30),  # not expiring → no refresh
        scope="daily",
    )
    provider.config.scopes = ["daily", "extra"]
    mock_store = AsyncMock()
    mock_store.read_token.return_value = drifted_token

    with caplog.at_level("WARNING"):
        await _run_loop_briefly({"test": provider}, mock_store, "test-ns")

    drift_warnings = [r for r in caplog.records if "Scope drift" in r.message]
    assert len(drift_warnings) == 1
    assert "extra" in drift_warnings[0].message


if __name__ == "__main__":
    pytest_bazel.main()
