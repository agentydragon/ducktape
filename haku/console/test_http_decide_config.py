"""Config and env-reference loading contracts of the egress decide endpoint's credentials."""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_bazel

from haku.console.http_decide_config import EgressDecideConfig, EgressFenceCredentialEntry, load_egress_decide

_AGENT = UUID("10000000-0000-4000-8000-000000000001")
_PROXY_TOKEN = "proxy-identity-token"
_FENCE = "agent-fence-credential"


def test_egress_decide_config_requires_distinct_env_references() -> None:
    with pytest.raises(ValueError, match="distinct"):
        EgressDecideConfig(
            proxy_token_env_var="EGRESS_TOKEN",
            fence_credentials=[EgressFenceCredentialEntry(agent_id=_AGENT, token_env_var="EGRESS_TOKEN")],
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


if __name__ == "__main__":
    pytest_bazel.main()
