"""History's Python settings mirror the Go service's ingress prerequisites."""

from typing import Any

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.history_service.settings import Settings


def _settings_source(
    *,
    sampler_identity_callers: bool = False,
    policy_namespace: str | None = "test-policies",
    sampler_accounts: list[dict[str, str]] | None = None,
    spiffe_listener: bool = True,
) -> dict[str, Any]:
    auth: list[dict[str, Any]] = [{"mode": "kubernetes_bearer", "audiences": ["test-history", "test-ingress"]}]
    if spiffe_listener:
        auth.append({"mode": "spiffe", "ca_file": "/test/workload-ca.crt"})
    return {
        "database_url": "postgresql://history:history@postgres.invalid/history",
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


def test_sampler_identity_callers_default_off() -> None:
    source = _settings_source()
    del source["ingress"]["sampler_identity_callers"]
    settings = Settings.model_validate(source)
    assert not settings.ingress.sampler_identity_callers


def test_sampler_identity_callers_accepts_all_go_prerequisites() -> None:
    settings = Settings.model_validate(_settings_source(sampler_identity_callers=True))
    assert settings.ingress.sampler_identity_callers


@pytest.mark.parametrize("missing", ["policy_namespace", "sampler_accounts", "spiffe_listener"])
def test_sampler_identity_callers_require_all_go_prerequisites(missing: str) -> None:
    source = _settings_source(sampler_identity_callers=True)
    if missing == "policy_namespace":
        source["policy_namespace"] = None
    elif missing == "sampler_accounts":
        source["ingress"]["sampler_accounts"] = []
    else:
        source["rpc"]["auth"] = [method for method in source["rpc"]["auth"] if method["mode"] != "spiffe"]

    with pytest.raises(ValidationError, match=r"ingress\.sampler_identity_callers requires"):
        Settings.model_validate(source)


if __name__ == "__main__":
    pytest_bazel.main()
