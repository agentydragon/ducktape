"""Integration test: k8s secrets via egress proxy in UDS mode (no TCP auth proxy).

Verifies that setup_k8s_secrets() works when no local TCP auth proxy exists. The
egress proxy URL (with embedded credentials) is passed directly. This exercises
normalize_proxy_url() extracting credentials into an explicit Proxy-Authorization
header, required for urllib3 v2 on HTTPS CONNECT tunnels.
"""

import base64
import http.server
import json
import ssl
import threading
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import docker.models.networks
import pytest
import pytest_bazel
import yaml

from devinfra.claude.hook_config import HookConfig, K8sConfig, K8sSecretMapping, K8sSecretsConfig
from devinfra.claude.hook_daemon.session_start.k8s_secrets import setup_k8s_secrets
from devinfra.claude.testing.mitmproxy_fixture import MitmproxyFixture
from devinfra.claude.testing.proxy_ca import generate_server_cert
from util.docker import get_docker_network_gateway
from util.net import pick_free_port, wait_for_port

pytest_plugins = ["devinfra.claude.testing.mitmproxy_fixture"]

_FAKE_SECRETS: dict[str, dict[str, str]] = {"github-token": {"token": "fake-github-token"}}


class _K8sHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        # /api/v1/namespaces/{ns}/secrets/{name}
        parts = self.path.strip("/").split("/")
        if len(parts) == 6 and parts[4] == "secrets":
            name = parts[5]
            data = _FAKE_SECRETS.get(name, {"key": "default-value"})
            encoded = {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}
            body = json.dumps({"apiVersion": "v1", "kind": "Secret", "data": encoded}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        pass  # suppress output


@dataclass(frozen=True)
class MockK8sServer:
    url: str


@pytest.fixture
def mock_k8s_server(tmp_path: Path, proxy_net: docker.models.networks.Network) -> Generator[MockK8sServer]:
    """HTTPS mock k8s API server on the host, reachable from mitmproxy via the bridge gateway."""
    gateway = get_docker_network_gateway(proxy_net)
    port = pick_free_port(host="0.0.0.0")

    cert_pem, key_pem = generate_server_cert(gateway)
    cert_file = tmp_path / "server.crt"
    key_file = tmp_path / "server.key"
    cert_file.write_bytes(cert_pem)
    key_file.write_bytes(key_pem)

    tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    tls_ctx.load_cert_chain(str(cert_file), str(key_file))

    server = http.server.HTTPServer(("0.0.0.0", port), _K8sHandler)
    server.socket = tls_ctx.wrap_socket(server.socket, server_side=True)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wait_for_port("127.0.0.1", port, timeout_secs=5)

    try:
        yield MockK8sServer(url=f"https://{gateway}:{port}")
    finally:
        server.shutdown()


@pytest.fixture
def ca_file(tmp_path: Path, mitmproxy_proxy: MitmproxyFixture) -> Path:
    path = tmp_path / "combined_ca.pem"
    path.write_bytes(mitmproxy_proxy.ca_cert_pem)
    return path


def test_k8s_secrets_via_egress_proxy_uds_mode(
    tmp_path: Path, ca_file: Path, mitmproxy_proxy: MitmproxyFixture, mock_k8s_server: MockK8sServer
) -> None:
    """setup_k8s_secrets succeeds through the egress proxy without a TCP auth proxy.

    In UDS mode there is no local TCP proxy — the egress proxy URL with embedded
    credentials is passed directly. normalize_proxy_url() must extract the credentials
    as an explicit Proxy-Authorization header so that urllib3 v2 sends them on the
    HTTPS CONNECT tunnel; without this the proxy returns 403.
    """
    config = HookConfig(
        k8s=K8sConfig(
            server=mock_k8s_server.url,
            service_account="test-sa",
            service_account_namespace="test-ns",
            namespace="test-ns",
        ),
        k8s_secrets=K8sSecretsConfig(
            namespace="test-ns", secrets=[K8sSecretMapping(name="github-token", data={"token": "GITHUB_TOKEN"})]
        ),
    )

    result = setup_k8s_secrets(
        token="test-service-account-token",
        session_dir=tmp_path,
        combined_ca_path=ca_file,
        config=config,
        proxy=mitmproxy_proxy.url,  # egress proxy URL with embedded credentials (UDS mode)
    )

    assert result.env_vars.get("GITHUB_TOKEN") == "fake-github-token"
    # Kubeconfig retains the full proxy URL with credentials (kubectl needs them)
    assert result.kubeconfig_path is not None
    kubeconfig = yaml.safe_load(result.kubeconfig_path.read_text())
    assert kubeconfig["clusters"][0]["cluster"]["proxy-url"] == mitmproxy_proxy.url


if __name__ == "__main__":
    pytest_bazel.main()
