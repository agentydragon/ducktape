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
    sampler_service_actor: str | None = None,
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
            "sampler_service_actor": sampler_service_actor,
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
    no_spiffe["reader_service_callers"] = ["spiffe://agentplane/app"]
    with pytest.raises(ValidationError, match="service callers require"):
        Settings.model_validate(no_spiffe)


def test_disabled_service_callers_are_absent_from_serialized_settings() -> None:
    serialized = Settings.model_validate(_settings_source()).model_dump(mode="json")
    assert "reader_service_callers" not in serialized
    assert "service_delegation" not in serialized
    assert "sampler_service_actor" not in serialized["ingress"]


def test_sampler_service_actor_requires_spiffe_listener() -> None:
    source = _settings_source(sampler_service_actor="spiffe://agentplane/central-sampler", spiffe_listener=False)
    with pytest.raises(ValidationError, match="service callers require"):
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
