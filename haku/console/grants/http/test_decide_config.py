"""Typed-settings contracts of the egress decide endpoint's credentials."""

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
    EgressConfigGrantEntry,
    EgressCredentialEntry,
    EgressDecideConfig,
    HttpOriginPattern,
    load_egress_decide,
)
from haku.console.grants.http.models import HttpMethod, HttpOrigin, HttpRequestCoverage, HttpScheme
from haku.console.grants.principal import AgentGrantPrincipal, SessionGrantPrincipal

_AGENT = UUID("10000000-0000-4000-8000-000000000001")
_DECISION_ENDPOINT_TOKEN = "shared-decision-endpoint-token"
_ORIGIN = HttpOrigin(scheme=HttpScheme.HTTPS, host="api.github.com", port=443)


def _credential_entry(**overrides: Any) -> EgressCredentialEntry:
    fields: dict[str, Any] = {
        "handle": "github-bot",
        "placeholder": "github-token-placeholder",
        "value": "ghp-real-value",
        "match_headers": frozenset({"Authorization"}),
        "principal": AgentGrantPrincipal(agent_id=_AGENT),
        "origins": frozenset({_ORIGIN}),
    }
    return EgressCredentialEntry(**{**fields, **overrides})


def test_egress_decide_config_requires_distinct_secret_values() -> None:
    config = EgressDecideConfig(
        decision_endpoint_token=_DECISION_ENDPOINT_TOKEN,
        credentials={"github_bot": _credential_entry(value=_DECISION_ENDPOINT_TOKEN)},
    )
    with pytest.raises(RuntimeError, match="duplicate"):
        load_egress_decide(config)


def test_egress_decide_config_accepts_typed_decision_endpoint_token() -> None:
    assert (
        EgressDecideConfig.model_validate(
            {"decision_endpoint_token": _DECISION_ENDPOINT_TOKEN}
        ).decision_endpoint_token.get_secret_value()
        == _DECISION_ENDPOINT_TOKEN
    )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        EgressDecideConfig.model_validate(
            {"unexpected_credentials": [], "decision_endpoint_token": _DECISION_ENDPOINT_TOKEN}
        )


def test_credential_entry_canonicalizes_and_validates_match_headers() -> None:
    assert _credential_entry(match_headers=frozenset({"Authorization", "X-Api-Key"})).match_headers == frozenset(
        {"authorization", "x-api-key"}
    )
    with pytest.raises(ValueError, match="header name"):
        _credential_entry(match_headers=frozenset({"not a header"}))


def test_credential_registry_requires_coherent_handles_and_placeholders() -> None:
    other = {"value": "other-real-value", "principal": AgentGrantPrincipal(agent_id=_AGENT)}
    with pytest.raises(ValueError, match="handles must be distinct"):
        EgressDecideConfig(
            decision_endpoint_token=_DECISION_ENDPOINT_TOKEN,
            credentials={
                "github_bot": _credential_entry(),
                "other": _credential_entry(placeholder="other-token-placeholder", **other),
            },
        )
    # A placeholder containing another would make the substring-swap substitutions order-dependent.
    with pytest.raises(ValueError, match="placeholder"):
        EgressDecideConfig(
            decision_endpoint_token=_DECISION_ENDPOINT_TOKEN,
            credentials={
                "github_bot": _credential_entry(),
                "github_bot_wide": _credential_entry(
                    handle="github-bot-wide", placeholder="github-token-placeholder-wide", **other
                ),
            },
        )


def test_prohibited_cidrs_parse_as_networks_and_default_empty() -> None:
    config = EgressDecideConfig.model_validate(
        {"decision_endpoint_token": _DECISION_ENDPOINT_TOKEN, "prohibited_cidrs": ["10.96.0.0/12", "fd00:10::/64"]}
    )
    assert config.prohibited_cidrs == frozenset({IPv4Network("10.96.0.0/12"), IPv6Network("fd00:10::/64")})
    assert EgressDecideConfig(decision_endpoint_token=_DECISION_ENDPOINT_TOKEN).prohibited_cidrs == frozenset()
    with pytest.raises(ValidationError):  # host bits set: an address, not a range
        EgressDecideConfig.model_validate(
            {"decision_endpoint_token": _DECISION_ENDPOINT_TOKEN, "prohibited_cidrs": ["10.96.0.1/12"]}
        )


def test_load_egress_decide_reads_typed_secret() -> None:
    config = EgressDecideConfig(decision_endpoint_token=_DECISION_ENDPOINT_TOKEN)
    loaded = load_egress_decide(config)
    assert loaded.decision_endpoint_token.get_secret_value() == _DECISION_ENDPOINT_TOKEN


def test_second_presentation_shares_the_value() -> None:
    """One credential, two presentations: two entries over one value, each with its own
    handle, placeholder, and match headers."""
    config = EgressDecideConfig(
        decision_endpoint_token=_DECISION_ENDPOINT_TOKEN,
        credentials={
            "github_bot": _credential_entry(),
            "github_bot_api_key": _credential_entry(
                handle="github-bot-api-key",
                placeholder="github-api-key-placeholder",
                match_headers=frozenset({"x-api-key"}),
            ),
        },
    )

    bearer, api_key = load_egress_decide(config).credentials

    assert bearer.value.get_secret_value() == api_key.value.get_secret_value() == "ghp-real-value"
    assert (bearer.placeholder, api_key.placeholder) == ("github-token-placeholder", "github-api-key-placeholder")
    assert api_key.match_headers == frozenset({"x-api-key"})


def _config_grant(**overrides: Any) -> EgressConfigGrantEntry:
    """Build a configuration grant; ``methods``/``path_regex`` overrides populate coverage."""
    coverage_fields: dict[str, Any] = {"methods": frozenset({HttpMethod.GET})}
    for key in ("methods", "path_regex"):
        if key in overrides:
            coverage_fields[key] = overrides.pop(key)
    fields: dict[str, Any] = {
        "id": "haku-github-api",
        "principal": AgentGrantPrincipal(agent_id=_AGENT),
        "origins": frozenset({_ORIGIN}),
        "coverage": HttpRequestCoverage(**coverage_fields),
    }
    return EgressConfigGrantEntry(**{**fields, **overrides})


def test_config_grant_entries_validate_fail_loud() -> None:
    with pytest.raises(ValueError, match="ids must be distinct"):
        EgressDecideConfig(
            decision_endpoint_token=_DECISION_ENDPOINT_TOKEN,
            grants=[_config_grant(), _config_grant(methods=frozenset({HttpMethod.POST}))],
        )
    with pytest.raises(ValueError, match="unknown credential handle"):
        EgressDecideConfig(
            decision_endpoint_token=_DECISION_ENDPOINT_TOKEN, grants=[_config_grant(credential_handle="ghost")]
        )
    with pytest.raises(ValueError, match="path_regex"):
        _config_grant(path_regex="([unclosed")
    with pytest.raises(ValueError, match="id"):
        _config_grant(id="Not A Slug")
    # Origins use the grant vocabulary, so ungrantable shapes are refused at parse time.
    with pytest.raises(ValueError, match="wildcard"):
        EgressConfigGrantEntry.model_validate(
            {
                "id": "wild",
                "principal": {"kind": "agent", "agent_id": str(_AGENT)},
                "origins": [{"scheme": "https", "host": "*.github.com", "port": 443}],
                "coverage": {"methods": ["GET"]},
            }
        )


def test_origin_pattern_fullmatches_host_at_exact_scheme_and_port() -> None:
    pattern = HttpOriginPattern(
        scheme=HttpScheme.HTTPS, host_pattern=r"productionresults[a-z0-9]*\.blob\.core\.windows\.net", port=443
    )
    fleet_host = "productionresultssa13.blob.core.windows.net"
    assert pattern.matches(HttpOrigin(scheme=HttpScheme.HTTPS, host=fleet_host, port=443))
    # Fullmatch, not search: a suffix-extended host under an unrelated registrable domain misses.
    assert not pattern.matches(HttpOrigin(scheme=HttpScheme.HTTPS, host=fleet_host + ".evil.example", port=443))
    assert not pattern.matches(HttpOrigin(scheme=HttpScheme.HTTPS, host=fleet_host, port=8443))
    assert not pattern.matches(HttpOrigin(scheme=HttpScheme.HTTP, host=fleet_host, port=443))


def test_pattern_only_grant_matches_origins_and_requires_some_origin() -> None:
    entry = _config_grant(
        origins=frozenset(),
        origin_patterns=frozenset(
            {HttpOriginPattern(scheme=HttpScheme.HTTPS, host_pattern=r"a[0-9]+\.example", port=443)}
        ),
    )
    assert entry.matches_origin(HttpOrigin(scheme=HttpScheme.HTTPS, host="a7.example", port=443))
    assert not entry.matches_origin(HttpOrigin(scheme=HttpScheme.HTTPS, host="b7.example", port=443))
    with pytest.raises(ValidationError, match="at least one origin"):
        _config_grant(origins=frozenset())
    with pytest.raises(ValidationError, match="not a valid regex"):
        HttpOriginPattern(scheme=HttpScheme.HTTPS, host_pattern=r"a[0-9.example", port=443)


def test_overlapping_configuration_grants_are_deliberately_legal() -> None:
    config = EgressDecideConfig(
        decision_endpoint_token=_DECISION_ENDPOINT_TOKEN,
        credentials={"github_bot": _credential_entry()},
        grants=[
            _config_grant(id="broad"),
            _config_grant(id="credentialed", path_regex="/repos/.*", credential_handle="github-bot"),
        ],
    )
    assert [entry.id for entry in config.grants] == ["broad", "credentialed"]


def test_config_grant_allow_prohibited_address_defaults_off_and_parses() -> None:
    assert _config_grant().allow_prohibited_address is False
    parsed = EgressConfigGrantEntry.model_validate(
        {
            "id": "internal-gateway",
            "principal": {"kind": "agent", "agent_id": str(_AGENT)},
            "origins": [{"scheme": "http", "host": "gateway.internal.example", "port": 4000}],
            "coverage": {"methods": ["POST"]},
            "allow_prohibited_address": True,
        }
    )
    assert parsed.allow_prohibited_address is True


def test_load_egress_decide_passes_configuration_grants_through() -> None:
    """Configuration grants carry no secrets, so loading preserves the reviewed entry."""
    config = EgressDecideConfig(
        decision_endpoint_token=_DECISION_ENDPOINT_TOKEN,
        credentials={"github_bot": _credential_entry()},
        grants=[_config_grant(credential_handle="github-bot")],
    )
    assert load_egress_decide(config).grants == config.grants


def test_github_api_and_git_grants_share_one_agent_credential() -> None:
    """Agent haku reaches the GitHub API and Git over one declared credential
    grants, redeeming the bot credential at both — Bearer on the API, git-over-HTTPS Basic on
    github.com (one registry entry: both are Authorization presentations of one placeholder).
    The deployed section is checked separately by cluster validation; this test keeps the parser
    and the representative in-process configuration independent of deployment files."""
    haku = UUID("8d5b0cba-a9ab-4c93-8c31-70d5c7af45c2")
    config = EgressDecideConfig.model_validate(
        yaml.safe_load(
            textwrap.dedent(
                """
                decision_endpoint_token: shared-decision-endpoint-token
                credentials:
                  github_bot:
                    handle: github-bot
                    placeholder: github-token-placeholder
                    value: ghp-real-value
                    match_headers: [authorization]
                    principal: {kind: agent, agent_id: 8d5b0cba-a9ab-4c93-8c31-70d5c7af45c2}
                    origins:
                      - {scheme: https, host: api.github.com, port: 443}
                      - {scheme: https, host: github.com, port: 443}
                grants:
                  - id: haku-github-api
                    principal: {kind: agent, agent_id: 8d5b0cba-a9ab-4c93-8c31-70d5c7af45c2}
                    origins:
                      - {scheme: https, host: api.github.com, port: 443}
                    coverage:
                      methods: [DELETE, GET, HEAD, PATCH, POST, PUT]
                    credential_handle: github-bot
                  - id: haku-github-git
                    principal: {kind: agent, agent_id: 8d5b0cba-a9ab-4c93-8c31-70d5c7af45c2}
                    origins:
                      - {scheme: https, host: github.com, port: 443}
                    coverage:
                      methods: [GET, POST]
                    credential_handle: github-bot
                """
            )
        )
    )
    loaded = load_egress_decide(config)

    api_entry, git_entry = loaded.grants
    assert (api_entry.id, git_entry.id) == ("haku-github-api", "haku-github-git")
    # Absent path_regex covers every path plus query — git smart HTTP needs
    # /info/refs?service=git-upload-pack through to the pack endpoints.
    assert git_entry.coverage.path_regex is None
    assert git_entry.coverage.methods == frozenset({HttpMethod.GET, HttpMethod.POST})
    # The registry binding must actually redeem what the configuration grants admit: same principal,
    # every grant origin within the credential's redemption origins.
    (credential,) = loaded.credentials
    for entry in loaded.grants:
        assert entry.credential_handle == credential.handle
        assert entry.principal == credential.principal == AgentGrantPrincipal(agent_id=haku)
        assert entry.origins <= credential.origins


def test_load_egress_credentials_present_value_conflicts_fail_loud() -> None:
    """Absence is tolerated (see the skip test), but a present-but-conflicting registry value is a
    misconfiguration or attack and still raises: it may not duplicate an identity secret nor equal a
    configured placeholder."""
    config = EgressDecideConfig(
        decision_endpoint_token=_DECISION_ENDPOINT_TOKEN,
        credentials={"github_bot": _credential_entry(value=_DECISION_ENDPOINT_TOKEN)},
    )
    with pytest.raises(RuntimeError, match="duplicate"):
        load_egress_decide(config)

    # A value equal to a configured placeholder would make the "inert" placeholder the secret.
    config = EgressDecideConfig(
        decision_endpoint_token=_DECISION_ENDPOINT_TOKEN,
        credentials={"github_bot": _credential_entry(value="github-token-placeholder")},
    )
    with pytest.raises(RuntimeError, match="placeholder"):
        load_egress_decide(config)

    config = EgressDecideConfig(
        decision_endpoint_token=_DECISION_ENDPOINT_TOKEN, credentials={"github_bot": _credential_entry()}
    )
    (loaded,) = load_egress_decide(config).credentials
    assert loaded.handle == "github-bot"
    assert loaded.placeholder == "github-token-placeholder"
    assert loaded.value.get_secret_value() == "ghp-real-value"
    assert loaded.match_headers == frozenset({"authorization"})
    assert loaded.principal == AgentGrantPrincipal(agent_id=_AGENT)
    assert loaded.origins == frozenset({_ORIGIN})


def test_configuration_entries_reject_session_principals() -> None:
    with pytest.raises(ValidationError):
        _config_grant(principal=SessionGrantPrincipal(session_id=UUID(int=1)))
    with pytest.raises(ValidationError):
        _credential_entry(principal=SessionGrantPrincipal(session_id=UUID(int=1)))


def test_load_egress_decide_skips_unprovisioned_credential(caplog: pytest.LogCaptureFixture) -> None:
    """An absent registry value is skipped without blocking provisioned entries."""
    config = EgressDecideConfig(
        decision_endpoint_token=_DECISION_ENDPOINT_TOKEN,
        credentials={
            "github_bot": _credential_entry(),
            "gitlab_bot": _credential_entry(handle="gitlab-bot", placeholder="gitlab-token-placeholder", value=None),
        },
    )

    with caplog.at_level(logging.WARNING):
        loaded = load_egress_decide(config)

    assert [credential.handle for credential in loaded.credentials] == ["github-bot"]
    assert any(record.levelno == logging.WARNING and "gitlab-bot" in record.getMessage() for record in caplog.records)


if __name__ == "__main__":
    pytest_bazel.main()
