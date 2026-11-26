"""The `policy_service` block accepts only the LLM ingress SVID calling the Policy Controller."""

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.llm_ingress.settings import Settings
from agentplane.net.peertls.config import LLM_INGRESS_URI, MCP_EDGE_URI, POLICY_CONTROLLER_URI

_BASE = {
    "allowed_service_account_namespaces": ["test-namespace"],
    "policy_namespace": "test-policy",
    "litellm_url": "http://litellm.test.invalid:4000",
    "log_levels": {},
    "rpc": {
        "listen_address": "127.0.0.1:6060",
        "tls": {"cert_file": "/tls/cert", "key_file": "/tls/key"},
        "auth": [{"mode": "spiffe", "ca_file": "/tls/ca"}],
    },
}
_TLS = {
    "ca_file": "/tls/ca",
    "server_uri": POLICY_CONTROLLER_URI,
    "cert_file": "/tls/cert",
    "key_file": "/tls/key",
    "client_uri": LLM_INGRESS_URI,
}
_POLICY_SERVICE = {"target": "policy-controller.test.invalid:8443", "tls": _TLS}


def test_policy_service_is_required() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(_BASE)


def test_policy_service_is_rendered() -> None:
    rendered = Settings.model_validate({**_BASE, "policy_service": _POLICY_SERVICE}).model_dump(mode="json")
    assert rendered["policy_service"]["tls"]["client_uri"] == LLM_INGRESS_URI


def test_policy_service_rejects_another_caller() -> None:
    peer = {**_POLICY_SERVICE, "tls": {**_TLS, "client_uri": MCP_EDGE_URI}}
    with pytest.raises(ValidationError):
        Settings.model_validate({**_BASE, "policy_service": peer})


if __name__ == "__main__":
    pytest_bazel.main()
