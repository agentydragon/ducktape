"""History's Python settings mirror the Go service's ingress prerequisites."""

from typing import Any

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.history_service.settings import Settings


def _settings_source(
    *,
    sampler_identity_callers: bool = False,
    policy_namespace: str = "test-policies",
    sampler_accounts: list[dict[str, str]] | None = None,
    spiffe_listener: bool = True,
) -> dict[str, Any]:
    auth: list[dict[str, Any]] = [{"mode": "kubernetes_bearer", "audiences": ["test-history", "test-ingress"]}]
    if spiffe_listener:
        auth.append({"mode": "spiffe", "ca_file": "/test/workload-ca.crt"})
    return {
        "reader_accounts": [{"namespace": "test-agentplane", "name": "test-reader"}],
        "token_audience": "test-history",
        "policy_namespace": policy_namespace,
        "rpc": {
            "listen_address": "0.0.0.0:8080",
            "tls": {"cert_file": "/test/history.crt", "key_file": "/test/history.key"},
            "auth": auth,
        },
        "ingress": {
            "token_audience": "test-ingress",
            "writer_namespaces": ["test-agentplane"],
            "sampler_accounts": sampler_accounts
            if sampler_accounts is not None
            else [{"namespace": "test-agentplane", "name": "test-sampler"}],
            "sampler_identity_callers": sampler_identity_callers,
        },
    }


@pytest.mark.parametrize("value", ["missing", "empty"])
def test_policy_namespace_is_required(value: str) -> None:
    source = _settings_source()
    if value == "missing":
        del source["policy_namespace"]
    else:
        source["policy_namespace"] = ""
    with pytest.raises(ValidationError):
        Settings.model_validate(source)


def test_sampler_identity_callers_default_off() -> None:
    source = _settings_source()
    del source["ingress"]["sampler_identity_callers"]
    settings = Settings.model_validate(source)
    assert not settings.ingress.sampler_identity_callers


def test_sampler_identity_callers_accepts_all_go_prerequisites() -> None:
    settings = Settings.model_validate(_settings_source(sampler_identity_callers=True))
    assert settings.ingress.sampler_identity_callers


@pytest.mark.parametrize("missing", ["sampler_accounts", "spiffe_listener"])
def test_sampler_identity_callers_require_all_go_prerequisites(missing: str) -> None:
    source = _settings_source(sampler_identity_callers=True)
    if missing == "sampler_accounts":
        source["ingress"]["sampler_accounts"] = []
    else:
        source["rpc"]["auth"] = [method for method in source["rpc"]["auth"] if method["mode"] != "spiffe"]

    with pytest.raises(ValidationError, match=r"ingress\.sampler_identity_callers requires"):
        Settings.model_validate(source)


def test_settings_reject_runtime_secrets_and_legacy_namespace_authority() -> None:
    source = _settings_source()
    source["database_url"] = "postgresql://secret.invalid/history"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Settings.model_validate(source)

    source = _settings_source()
    source["delegation"] = {
        "actor": "test-agentplane/actions",
        "identity_callers": False,
        "methods": ["/agentplane.history_service.HistoryService/ReadSessionEvents"],
    }
    with pytest.raises(ValidationError):
        Settings.model_validate(source)

    source = _settings_source()
    source["delegation"] = {
        "actor": "test-agentplane/actions",
        "identity_callers": True,
        "methods": ["/agentplane.history_service.HistoryService/ReadSessionEvents"],
        "caller_namespaces": ["test-agentplane"],
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Settings.model_validate(source)


def test_file_settings_do_not_read_static_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTPLANE_HISTORY_SERVICE_TOKEN_AUDIENCE", "environment-value")
    settings = Settings.model_validate(_settings_source())
    assert settings.token_audience == "test-history"
    assert "database_url" not in Settings.model_fields
    assert Settings.config_file_env == "AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE"


if __name__ == "__main__":
    pytest_bazel.main()
