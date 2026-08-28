"""Config and env-reference loading contracts of the egress decide endpoint's credentials."""

from __future__ import annotations

import logging
import textwrap
from ipaddress import IPv4Network, IPv6Network
from typing import Any
from uuid import UUID

import pytest
import pytest_bazel
import yaml
from pydantic import ValidationError

from haku.console.grants.http.decide_config import (
    EgressCredentialEntry,
    EgressDecideConfig,
    EgressFenceCredentialEntry,
    EgressStandingPolicyEntry,
    load_egress_decide,
)
from haku.console.grants.http.models import HttpMethod, HttpOrigin, HttpRequestCoverage, HttpScheme

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


def test_prohibited_cidrs_parse_as_networks_and_default_empty() -> None:
    config = EgressDecideConfig.model_validate(
        {"proxy_token_env_var": "EGRESS_TOKEN", "prohibited_cidrs": ["10.96.0.0/12", "fd00:10::/64"]}
    )
    assert config.prohibited_cidrs == frozenset({IPv4Network("10.96.0.0/12"), IPv6Network("fd00:10::/64")})
    assert EgressDecideConfig(proxy_token_env_var="EGRESS_TOKEN").prohibited_cidrs == frozenset()
    with pytest.raises(ValidationError):  # host bits set: an address, not a range
        EgressDecideConfig.model_validate({"proxy_token_env_var": "EGRESS_TOKEN", "prohibited_cidrs": ["10.96.0.1/12"]})


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


def _standing_entry(**overrides: Any) -> EgressStandingPolicyEntry:
    """Build a standing entry; ``methods``/``path_regex`` overrides populate the nested coverage."""
    coverage_fields: dict[str, Any] = {"methods": frozenset({HttpMethod.GET})}
    for key in ("methods", "path_regex"):
        if key in overrides:
            coverage_fields[key] = overrides.pop(key)
    fields: dict[str, Any] = {
        "id": "haku-github-api",
        "agent_ids": frozenset({_AGENT}),
        "origins": frozenset({_ORIGIN}),
        "coverage": HttpRequestCoverage(**coverage_fields),
    }
    return EgressStandingPolicyEntry(**{**fields, **overrides})


def test_standing_policy_entries_validate_fail_loud() -> None:
    with pytest.raises(ValueError, match="ids must be distinct"):
        EgressDecideConfig(
            proxy_token_env_var="EGRESS_TOKEN",
            standing_policies=[_standing_entry(), _standing_entry(methods=frozenset({HttpMethod.POST}))],
        )
    with pytest.raises(ValueError, match="unknown credential handle"):
        EgressDecideConfig(
            proxy_token_env_var="EGRESS_TOKEN", standing_policies=[_standing_entry(credential_handle="ghost")]
        )
    with pytest.raises(ValueError, match="path_regex"):
        _standing_entry(path_regex="([unclosed")
    with pytest.raises(ValueError, match="id"):
        _standing_entry(id="Not A Slug")
    # Origins use the grant vocabulary, so ungrantable shapes are refused at parse time.
    with pytest.raises(ValueError, match="wildcard"):
        EgressStandingPolicyEntry.model_validate(
            {
                "id": "wild",
                "agent_ids": [str(_AGENT)],
                "origins": [{"scheme": "https", "host": "*.github.com", "port": 443}],
                "coverage": {"methods": ["GET"]},
            }
        )


def test_overlapping_standing_entries_are_deliberately_legal() -> None:
    config = EgressDecideConfig(
        proxy_token_env_var="EGRESS_TOKEN",
        credentials=[_credential_entry()],
        standing_policies=[
            _standing_entry(id="broad"),
            _standing_entry(id="credentialed", path_regex="/repos/.*", credential_handle="github-bot"),
        ],
    )
    assert [entry.id for entry in config.standing_policies] == ["broad", "credentialed"]


def test_standing_entry_allow_prohibited_address_defaults_off_and_parses() -> None:
    assert _standing_entry().allow_prohibited_address is False
    parsed = EgressStandingPolicyEntry.model_validate(
        {
            "id": "internal-gateway",
            "agent_ids": [str(_AGENT)],
            "origins": [{"scheme": "http", "host": "gateway.internal.example", "port": 4000}],
            "coverage": {"methods": ["POST"]},
            "allow_prohibited_address": True,
        }
    )
    assert parsed.allow_prohibited_address is True


def test_load_egress_decide_passes_standing_policies_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """Standing entries carry no secrets, so the loaded view is the literal reviewed entry."""
    config = EgressDecideConfig(
        proxy_token_env_var="EGRESS_PROXY_TOKEN",
        credentials=[_credential_entry()],
        standing_policies=[_standing_entry(credential_handle="github-bot")],
    )
    monkeypatch.setenv("EGRESS_PROXY_TOKEN", _PROXY_TOKEN)
    monkeypatch.setenv("EGRESS_CREDENTIAL_GITHUB_BOT", "ghp-real-value")

    assert load_egress_decide(config).standing_policies == config.standing_policies


def test_github_spike_standing_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The #4943 GitHub spike shape: Agent haku reaches api.github.com + github.com under standing
    policy, redeeming the bot credential at both — Bearer on the API, git-over-HTTPS Basic on
    github.com (one registry entry: both are Authorization presentations of one placeholder).
    The deployed section lives in cluster/k8s/haku/console/config.yaml; its coherence with the
    registry is asserted over the real file in test_deployment_config.py."""
    haku = UUID("8d5b0cba-a9ab-4c93-8c31-70d5c7af45c2")
    config = EgressDecideConfig.model_validate(
        yaml.safe_load(
            textwrap.dedent(
                """
                proxy_token_env_var: HAKU_EGRESS_PROXY_TOKEN
                fence_credentials:
                  - agent_id: 8d5b0cba-a9ab-4c93-8c31-70d5c7af45c2
                    token_env_var: HAKU_EGRESS_FENCE_HAKU
                credentials:
                  - handle: github-bot
                    placeholder: github-token-placeholder
                    value_env_var: HAKU_EGRESS_CREDENTIAL_GITHUB_BOT
                    match_headers: [authorization]
                    agent_ids: [8d5b0cba-a9ab-4c93-8c31-70d5c7af45c2]
                    origins:
                      - {scheme: https, host: api.github.com, port: 443}
                      - {scheme: https, host: github.com, port: 443}
                standing_policies:
                  - id: haku-github-api
                    agent_ids: [8d5b0cba-a9ab-4c93-8c31-70d5c7af45c2]
                    origins:
                      - {scheme: https, host: api.github.com, port: 443}
                    coverage:
                      methods: [DELETE, GET, HEAD, PATCH, POST, PUT]
                    credential_handle: github-bot
                  - id: haku-github-git
                    agent_ids: [8d5b0cba-a9ab-4c93-8c31-70d5c7af45c2]
                    origins:
                      - {scheme: https, host: github.com, port: 443}
                    coverage:
                      methods: [GET, POST]
                    credential_handle: github-bot
                """
            )
        )
    )
    monkeypatch.setenv("HAKU_EGRESS_PROXY_TOKEN", _PROXY_TOKEN)
    monkeypatch.setenv("HAKU_EGRESS_FENCE_HAKU", _FENCE)
    monkeypatch.setenv("HAKU_EGRESS_CREDENTIAL_GITHUB_BOT", "ghp-real-value")
    loaded = load_egress_decide(config)

    api_entry, git_entry = loaded.standing_policies
    assert (api_entry.id, git_entry.id) == ("haku-github-api", "haku-github-git")
    # Absent path_regex covers every path plus query — git smart HTTP needs
    # /info/refs?service=git-upload-pack through to the pack endpoints.
    assert git_entry.coverage.path_regex is None
    assert git_entry.coverage.methods == frozenset({HttpMethod.GET, HttpMethod.POST})
    # The registry binding must actually redeem what the standing entries admit: same Agent set,
    # every standing origin within the credential's redemption origins.
    (credential,) = loaded.credentials
    for entry in loaded.standing_policies:
        assert entry.credential_handle == credential.handle
        assert entry.agent_ids <= credential.agent_ids == frozenset({haku})
        assert entry.origins <= credential.origins


def test_load_egress_credentials_present_value_conflicts_fail_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absence is tolerated (see the skip test), but a present-but-conflicting registry value is a
    misconfiguration or attack and still raises: it may not duplicate an identity secret nor equal a
    configured placeholder."""
    config = EgressDecideConfig(proxy_token_env_var="EGRESS_PROXY_TOKEN", credentials=[_credential_entry()])
    monkeypatch.setenv("EGRESS_PROXY_TOKEN", _PROXY_TOKEN)

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


def test_load_egress_decide_skips_credential_with_unset_env_var(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A registry credential whose value env var is unset is skipped with a warning, not fatal: the
    endpoint still loads the proxy token and every credential whose var is set."""
    config = EgressDecideConfig(
        proxy_token_env_var="EGRESS_PROXY_TOKEN",
        credentials=[
            _credential_entry(),
            _credential_entry(
                handle="gitlab-bot",
                placeholder="gitlab-token-placeholder",
                value_env_var="EGRESS_CREDENTIAL_GITLAB_BOT",
            ),
        ],
    )
    monkeypatch.setenv("EGRESS_PROXY_TOKEN", _PROXY_TOKEN)
    monkeypatch.setenv("EGRESS_CREDENTIAL_GITHUB_BOT", "ghp-real-value")
    monkeypatch.delenv("EGRESS_CREDENTIAL_GITLAB_BOT", raising=False)

    with caplog.at_level(logging.WARNING):
        loaded = load_egress_decide(config)

    assert [credential.handle for credential in loaded.credentials] == ["github-bot"]
    assert any(
        record.levelno == logging.WARNING and "EGRESS_CREDENTIAL_GITLAB_BOT" in record.getMessage()
        for record in caplog.records
    )


if __name__ == "__main__":
    pytest_bazel.main()
