import pytest
import pytest_bazel
from pydantic import SecretStr, ValidationError

from finance.plaid.spend.settings import SpendSettings

_REQUIRED_ENV = {
    "DATABASE_URL": "postgresql://plaid_spend:test@localhost/plaidmcp",
    "PLAID_SPEND_API_OIDC_ISSUER": "https://auth.example.test/api/",
    "PLAID_SPEND_API_OIDC_CLIENT_ID": "plaid-spend-test",
    "PLAID_SPEND_API_OIDC_DISCOVERED_ISSUER": "https://auth.example.test/api/",
    "PLAID_SPEND_API_OIDC_JWKS_URI": "https://auth.example.test/api/jwks/",
    "PLAID_SPEND_WEB_OIDC_ISSUER": "https://auth.example.test/web/",
    "PLAID_SPEND_WEB_OIDC_PUBLIC_BASE_URL": "https://spend.example.test",
    "PLAID_SPEND_WEB_OIDC_CLIENT_ID": "web-test",
    "PLAID_SPEND_WEB_OIDC_CLIENT_SECRET": "web-secret-test",
    "PLAID_SPEND_WEB_OIDC_SESSION_SECRET": "session-secret-test",
}


def _settings(monkeypatch: pytest.MonkeyPatch, **values: str) -> SpendSettings:
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    for name in ("PLAID_SPEND_COINBASE__API_KEY", "PLAID_SPEND_COINBASE__API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return SpendSettings(_env_file=None)


def test_coinbase_credentials_are_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)

    assert settings.coinbase is None


def test_coinbase_credentials_are_both_secret_str_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        monkeypatch,
        PLAID_SPEND_COINBASE__API_KEY="view-only-key-test",
        PLAID_SPEND_COINBASE__API_SECRET="view-only-secret-test",
    )

    assert settings.coinbase is not None
    assert isinstance(settings.coinbase.api_key, SecretStr)
    assert isinstance(settings.coinbase.api_secret, SecretStr)
    assert settings.coinbase.api_key.get_secret_value() == "view-only-key-test"
    assert settings.coinbase.api_secret.get_secret_value() == "view-only-secret-test"


@pytest.mark.parametrize(
    "values",
    [
        {"PLAID_SPEND_COINBASE__API_KEY": "view-only-key-test"},
        {"PLAID_SPEND_COINBASE__API_SECRET": "view-only-secret-test"},
        {"PLAID_SPEND_COINBASE__API_KEY": "", "PLAID_SPEND_COINBASE__API_SECRET": "view-only-secret-test"},
        {"PLAID_SPEND_COINBASE__API_KEY": "  ", "PLAID_SPEND_COINBASE__API_SECRET": "view-only-secret-test"},
        {"PLAID_SPEND_COINBASE__API_KEY": "view-only-key-test", "PLAID_SPEND_COINBASE__API_SECRET": ""},
        {"PLAID_SPEND_COINBASE__API_KEY": "view-only-key-test", "PLAID_SPEND_COINBASE__API_SECRET": "  "},
    ],
)
def test_coinbase_credentials_reject_partial_or_empty_values(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, str]
) -> None:
    with pytest.raises(ValidationError) as error:
        _settings(monkeypatch, **values)

    error_text = str(error.value)
    assert not any(value in error_text for value in values.values() if value.strip())


if __name__ == "__main__":
    pytest_bazel.main()
