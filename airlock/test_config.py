import pytest
import pytest_bazel

from airlock.config import build_oauth_providers
from airlock.oauth.provider import OAuth2ProviderConfig, OAuthConfig, TokenSecretConfig


def test_hyphenated_provider_reads_underscored_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_WRITE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("TEST_WRITE_CLIENT_SECRET", "test-client-secret")
    oauth = OAuthConfig(
        target_namespace="airlock-test",
        providers=[
            OAuth2ProviderConfig(
                name="test-write",
                display_name="Test Provider (write)",
                authorize_url="https://provider.test.example/authorize",
                token_url="https://provider.test.example/token",
                scopes=["write"],
                refresh_secret=TokenSecretConfig(name="test-write-refresh"),
                access_secret=TokenSecretConfig(name="test-write-access"),
            )
        ],
    )

    provider = build_oauth_providers(oauth, "https://airlock.test.example/oauth/callback")["test-write"]

    assert (provider.client_id, provider.client_secret) == ("test-client-id", "test-client-secret")


if __name__ == "__main__":
    pytest_bazel.main()
