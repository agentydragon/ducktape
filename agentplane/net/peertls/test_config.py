"""PolicyServicePeer accepts only a service-SVID caller of the Policy Controller."""

from pathlib import Path

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.net.peertls.config import ACTIONS_URI, MCP_EDGE_URI, POLICY_CONTROLLER_URI, PolicyServicePeer

_SVID_TLS = {
    "ca_file": Path("/etc/agentplane/ca/ca.crt"),
    "server_uri": POLICY_CONTROLLER_URI,
    "cert_file": Path("/var/run/secrets/fake/tls.crt"),
    "key_file": Path("/var/run/secrets/fake/tls.key"),
    "client_uri": MCP_EDGE_URI,
}


def test_policy_service_peer_accepts_an_svid_caller() -> None:
    assert PolicyServicePeer.model_validate({"target": "policy-controller:8443", "tls": _SVID_TLS}).tls.uses_svid


@pytest.mark.parametrize(
    "peer",
    [
        {"target": "", "tls": _SVID_TLS},
        {"target": "policy-controller:8443", "tls": {**_SVID_TLS, "server_uri": ACTIONS_URI}},
        {
            "target": "policy-controller:8443",
            "tls": {"ca_file": _SVID_TLS["ca_file"], "server_uri": POLICY_CONTROLLER_URI},
        },
    ],
)
def test_policy_service_peer_rejects_incomplete_or_wrong_peers(peer: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PolicyServicePeer.model_validate(peer)


if __name__ == "__main__":
    pytest_bazel.main()
