"""History settings require live workload SVID trust."""

from typing import Any

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.history_service.settings import Settings
from agentplane.net.peertls.config import APP_URI, HISTORY_SERVICE_URI, POLICY_CONTROLLER_URI

_POLICY_SERVICE_TLS = {
    "ca_file": "/test/workload-ca.crt",
    "server_uri": POLICY_CONTROLLER_URI,
    "cert_file": "/test/history.crt",
    "key_file": "/test/history.key",
    "client_uri": HISTORY_SERVICE_URI,
}


def _settings_source(*, sampler_service_actor: str | None = None) -> dict[str, Any]:
    return {
        "policy_service": {"target": "test-policy-controller:8443", "tls": _POLICY_SERVICE_TLS},
        "rpc": {
            "listen_address": "0.0.0.0:8080",
            "tls": {"cert_file": "/test/history.crt", "key_file": "/test/history.key"},
            "auth": [{"mode": "spiffe", "ca_file": "/test/workload-ca.crt"}],
        },
        "ingress": {"writer_namespaces": ["test-agentplane"], "sampler_service_actor": sampler_service_actor},
    }


def test_policy_service_is_required() -> None:
    source = _settings_source()
    del source["policy_service"]
    with pytest.raises(ValidationError):
        Settings.model_validate(source)


def test_settings_reject_runtime_secrets() -> None:
    source = _settings_source()
    source["database_url"] = "postgresql://secret.invalid/history"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Settings.model_validate(source)


def test_service_callers_require_exact_service_ids() -> None:
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


def test_rpc_auth_requires_a_spiffe_method() -> None:
    source = _settings_source()
    source["rpc"]["auth"] = []
    with pytest.raises(ValidationError) as rejected:
        Settings.model_validate(source)
    assert [error["loc"] for error in rejected.value.errors()] == [("rpc", "auth")]


def test_disabled_service_callers_are_absent_from_serialized_settings() -> None:
    settings = Settings.model_validate(_settings_source())
    assert not settings.ingress.compact_execution_evidence
    serialized = settings.model_dump(mode="json")
    assert "reader_service_callers" not in serialized
    assert "service_delegation" not in serialized
    assert "sampler_service_actor" not in serialized["ingress"]
    assert "compact_execution_evidence" not in serialized["ingress"]
    assert "policy_namespace" not in serialized


def test_policy_service_is_reached_as_history() -> None:
    source = _settings_source()
    assert Settings.model_validate(source).model_dump(mode="json")["policy_service"]["target"] == (
        "test-policy-controller:8443"
    )
    source["policy_service"] = {
        "target": "test-policy-controller:8443",
        "tls": {**_POLICY_SERVICE_TLS, "client_uri": APP_URI},
    }
    with pytest.raises(ValidationError):
        Settings.model_validate(source)


def test_execution_evidence_compaction_can_be_enabled_explicitly() -> None:
    source = _settings_source()
    source["ingress"]["compact_execution_evidence"] = True
    settings = Settings.model_validate(source)
    assert settings.ingress.compact_execution_evidence
    assert settings.model_dump(mode="json")["ingress"]["compact_execution_evidence"]


def test_file_settings_do_not_read_static_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTPLANE_HISTORY_SERVICE_WATCH_LEASE_S", "1")
    settings = Settings.model_validate(_settings_source())
    assert settings.watch_lease_s == 300


if __name__ == "__main__":
    pytest_bazel.main()
