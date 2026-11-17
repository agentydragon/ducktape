"""History settings require live workload SVID trust and reject retired bearer keys."""

from typing import Any

import pytest
from pydantic import ValidationError

from agentplane.history_service.settings import Settings


def _settings_source(
    *, policy_namespace: str = "test-policies", sampler_service_actor: str | None = None, spiffe_listener: bool = True
) -> dict[str, Any]:
    auth = (
        [{"mode": "spiffe", "ca_file": "/test/workload-ca.crt"}]
        if spiffe_listener
        else [{"mode": "kubernetes_bearer", "audiences": ["legacy-reader"]}]
    )
    return {
        "policy_namespace": policy_namespace,
        "rpc": {
            "listen_address": "0.0.0.0:8080",
            "tls": {"cert_file": "/test/history.crt", "key_file": "/test/history.key"},
            "auth": auth,
        },
        "ingress": {"writer_namespaces": ["test-agentplane"], "sampler_service_actor": sampler_service_actor},
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


@pytest.mark.parametrize(
    ("scope", "key", "value"),
    [
        ("reader", "reader_accounts", []),
        ("reader", "delegation", {"actor": "test-agentplane/actions", "identity_callers": True, "methods": []}),
        ("reader", "token_audience", "legacy-reader"),
        ("ingress", "token_audience", "legacy-ingress"),
        ("ingress", "sampler_accounts", []),
        ("ingress", "sampler_identity_callers", True),
    ],
)
def test_retired_bearer_settings_are_unknown(scope: str, key: str, value: Any) -> None:
    source = _settings_source()
    target = source["ingress"] if scope == "ingress" else source
    target[key] = value
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Settings.model_validate(source)


def test_settings_reject_runtime_secrets() -> None:
    source = _settings_source()
    source["database_url"] = "postgresql://secret.invalid/history"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Settings.model_validate(source)


def test_service_callers_require_exact_service_ids_and_spiffe_listener() -> None:
    source = _settings_source()
    source["reader_service_callers"] = ["spiffe://agentplane/app", "spiffe://agentplane/notifications"]
    source["service_delegation"] = {
        "actors": [{"spiffe_id": "spiffe://agentplane/actions"}],
        "methods": ["/agentplane.history_service.HistoryService/ReadSessionEvents"],
    }
    source["ingress"]["sampler_service_actor"] = "spiffe://agentplane/central-sampler"
    settings = Settings.model_validate(source)
    assert settings.reader_service_callers == {"spiffe://agentplane/app", "spiffe://agentplane/notifications"}
    assert settings.service_delegation is not None
    assert settings.service_delegation.actors[0].spiffe_id == "spiffe://agentplane/actions"
    assert settings.ingress.sampler_service_actor == "spiffe://agentplane/central-sampler"

    for field, invalid in (
        ("reader_service_callers", ["spiffe://agentplane/agents/worker"]),
        ("service_delegation", {"actors": [{"spiffe_id": "spiffe://agentplane/agents/worker"}], "methods": ["x"]}),
    ):
        invalid_source = _settings_source()
        invalid_source[field] = invalid
        with pytest.raises(ValidationError):
            Settings.model_validate(invalid_source)

    no_spiffe = _settings_source(spiffe_listener=False)
    with pytest.raises(ValidationError, match="does not match any of the expected tags"):
        Settings.model_validate(no_spiffe)


def test_disabled_service_callers_are_absent_from_serialized_settings() -> None:
    serialized = Settings.model_validate(_settings_source()).model_dump(mode="json")
    assert "reader_service_callers" not in serialized
    assert "service_delegation" not in serialized
    assert "sampler_service_actor" not in serialized["ingress"]


def test_file_settings_do_not_read_static_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTPLANE_HISTORY_SERVICE_POLICY_NAMESPACE", "environment-value")
    settings = Settings.model_validate(_settings_source())
    assert settings.policy_namespace == "test-policies"
    assert "database_url" not in Settings.model_fields
    assert Settings.config_file_env == "AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE"
