"""Config and env-reference loading contracts of the egress decide endpoint's credentials."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
import pytest_bazel

from haku.console.http_decide_config import (
    EgressCredentialEntry,
    EgressDecideConfig,
    EgressFenceCredentialEntry,
    load_egress_decide,
)
from haku.console.http_grant_models import HttpOrigin, HttpScheme

_AGENT = UUID("10000000-0000-4000-8000-000000000001")
_PROXY_TOKEN = "proxy-identity-token"
_FENCE = "agent-fence-credential"
_ORIGIN = HttpOrigin(scheme=HttpScheme.HTTPS, host="api.github.com", port=443)


def _credential_entry(**overrides: Any) -> EgressCredentialEntry:
    fields: dict[str, Any] = {
        "handle": "github-bot",
        "placeholder": "github-token-placeholder",
        "value_env_var": "EGRESS_CREDENTIAL_GITHUB_BOT",
        "match_headers": frozenset({"Authorization"}),
        "agent_ids": frozenset({_AGENT}),
        "origins": frozenset({_ORIGIN}),
    }
    return EgressCredentialEntry(**{**fields, **overrides})


def test_egress_decide_config_requires_distinct_env_references() -> None:
    with pytest.raises(ValueError, match="distinct"):
        EgressDecideConfig(
            proxy_token_env_var="EGRESS_TOKEN",
            fence_credentials=[EgressFenceCredentialEntry(agent_id=_AGENT, token_env_var="EGRESS_TOKEN")],
        )
    with pytest.raises(ValueError, match="identity secrets"):
        EgressDecideConfig(
            proxy_token_env_var="EGRESS_TOKEN", credentials=[_credential_entry(value_env_var="EGRESS_TOKEN")]
        )


def test_credential_entry_canonicalizes_and_validates_match_headers() -> None:
    assert _credential_entry(match_headers=frozenset({"Authorization", "X-Api-Key"})).match_headers == frozenset(
        {"authorization", "x-api-key"}
    )
    with pytest.raises(ValueError, match="header name"):
        _credential_entry(match_headers=frozenset({"not a header"}))


def test_credential_registry_requires_coherent_handles_and_placeholders() -> None:
    other = {"value_env_var": "EGRESS_CREDENTIAL_OTHER", "agent_ids": frozenset({_AGENT})}
    with pytest.raises(ValueError, match="handles must be distinct"):
        EgressDecideConfig(
            proxy_token_env_var="EGRESS_TOKEN",
            credentials=[_credential_entry(), _credential_entry(placeholder="other-token-placeholder", **other)],
        )
    # A placeholder containing another would make the substring-swap substitutions order-dependent.
    with pytest.raises(ValueError, match="placeholder"):
        EgressDecideConfig(
            proxy_token_env_var="EGRESS_TOKEN",
            credentials=[
                _credential_entry(),
                _credential_entry(handle="github-bot-wide", placeholder="github-token-placeholder-wide", **other),
            ],
        )


def test_load_egress_decide_reads_env_references_and_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    config = EgressDecideConfig(
        proxy_token_env_var="EGRESS_PROXY_TOKEN",
        fence_credentials=[EgressFenceCredentialEntry(agent_id=_AGENT, token_env_var="EGRESS_FENCE_A")],
    )
    monkeypatch.delenv("EGRESS_PROXY_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="EGRESS_PROXY_TOKEN"):
        load_egress_decide(config)

    monkeypatch.setenv("EGRESS_PROXY_TOKEN", _PROXY_TOKEN)
    with pytest.raises(RuntimeError, match="EGRESS_FENCE_A"):
        load_egress_decide(config)

    monkeypatch.setenv("EGRESS_FENCE_A", _PROXY_TOKEN)
    with pytest.raises(RuntimeError, match="duplicate"):
        load_egress_decide(config)

    monkeypatch.setenv("EGRESS_FENCE_A", _FENCE)
    loaded = load_egress_decide(config)
    assert loaded.proxy_token.get_secret_value() == _PROXY_TOKEN
    (credential,) = loaded.fence_credentials
    assert credential.agent_id == _AGENT
    assert credential.token.get_secret_value() == _FENCE


def test_second_presentation_shares_the_value_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """One credential, two presentations: two entries over one env reference, each with its own
    handle, placeholder, and match headers."""
    config = EgressDecideConfig(
        proxy_token_env_var="EGRESS_PROXY_TOKEN",
        credentials=[
            _credential_entry(),
            _credential_entry(
                handle="github-bot-api-key",
                placeholder="github-api-key-placeholder",
                match_headers=frozenset({"x-api-key"}),
            ),
        ],
    )
    monkeypatch.setenv("EGRESS_PROXY_TOKEN", _PROXY_TOKEN)
    monkeypatch.setenv("EGRESS_CREDENTIAL_GITHUB_BOT", "ghp-real-value")

    bearer, api_key = load_egress_decide(config).credentials

    assert bearer.value.get_secret_value() == api_key.value.get_secret_value() == "ghp-real-value"
    assert (bearer.placeholder, api_key.placeholder) == ("github-token-placeholder", "github-api-key-placeholder")
    assert api_key.match_headers == frozenset({"x-api-key"})


def test_load_egress_credentials_reads_env_references_and_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    config = EgressDecideConfig(proxy_token_env_var="EGRESS_PROXY_TOKEN", credentials=[_credential_entry()])
    monkeypatch.setenv("EGRESS_PROXY_TOKEN", _PROXY_TOKEN)
    monkeypatch.delenv("EGRESS_CREDENTIAL_GITHUB_BOT", raising=False)
    with pytest.raises(RuntimeError, match="EGRESS_CREDENTIAL_GITHUB_BOT"):
        load_egress_decide(config)

    monkeypatch.setenv("EGRESS_CREDENTIAL_GITHUB_BOT", _PROXY_TOKEN)
    with pytest.raises(RuntimeError, match="duplicate"):
        load_egress_decide(config)

    # A value equal to a configured placeholder would make the "inert" placeholder the secret.
    monkeypatch.setenv("EGRESS_CREDENTIAL_GITHUB_BOT", "github-token-placeholder")
    with pytest.raises(RuntimeError, match="placeholder"):
        load_egress_decide(config)

    monkeypatch.setenv("EGRESS_CREDENTIAL_GITHUB_BOT", "ghp-real-value")
    (loaded,) = load_egress_decide(config).credentials
    assert loaded.handle == "github-bot"
    assert loaded.placeholder == "github-token-placeholder"
    assert loaded.value.get_secret_value() == "ghp-real-value"
    assert loaded.match_headers == frozenset({"authorization"})
    assert loaded.agent_ids == frozenset({_AGENT})
    assert loaded.origins == frozenset({_ORIGIN})


if __name__ == "__main__":
    pytest_bazel.main()
