"""Integration test: k8s secrets via egress proxy in UDS mode (no TCP auth proxy).

Verifies that read_k8s_secret() works when no local TCP auth proxy exists. The
egress proxy URL (with embedded credentials) is passed directly. This exercises
normalize_proxy_url() extracting credentials into an explicit Proxy-Authorization
header, required for urllib3 v2 on HTTPS CONNECT tunnels.

All components run as containers on Docker bridge networks:
- mitmproxy: TLS-intercepting proxy with Basic auth
- mock k8s API: self-signed HTTPS server responding to Secret API
- test client: kubernetes Python client with normalize_proxy_url

Host→container networking is unreliable on Firecracker microVMs (RBE workers)
due to missing iptables support, so everything runs container-to-container.
"""

import json
import logging
import os
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import docker
import docker.models.containers
import docker.models.networks
import pytest
import pytest_bazel

from devinfra.claude.testing.mitmproxy_fixture import MitmproxyFixture
from util.oci import load_image
from util.testing.undeclared_outputs import undeclared_outputs_dir

pytest_plugins = ["devinfra.claude.testing.mitmproxy_fixture"]

logger = logging.getLogger(__name__)

_FAKE_SECRETS: dict[str, dict[str, str]] = {"github-token": {"token": "fake-github-token"}}
_MOCK_K8S_PORT = 6444
_PROXY_CREDENTIALS = "proxy_user:test_jwt_token"

_MOCK_K8S_IMAGE = "mock-k8s-server:pinned"
_MOCK_K8S_TARBALL = "_main/devinfra/claude/hook_daemon/session_start/mock_k8s_server_load/tarball.tar"
_MOCK_K8S_ALIAS = "mock-k8s"

_CLIENT_IMAGE = "k8s-test-client:pinned"
_CLIENT_TARBALL = "_main/devinfra/claude/hook_daemon/session_start/k8s_test_client_load/tarball.tar"


def _get_container_ip(container: docker.models.containers.Container, network_name: str) -> str:
    container.reload()
    net = container.attrs["NetworkSettings"]["Networks"].get(network_name, {})
    ip: str = net.get("IPAddress", "")
    if not ip:
        raise RuntimeError(f"Container {container.name} has no IP on network {network_name}")
    return ip


def _save_output(name: str, content: str) -> None:
    out_dir = undeclared_outputs_dir() / "k8s-proxy-integration"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_text(content)


@dataclass(frozen=True)
class MockK8sServer:
    url: str
    container: docker.models.containers.Container


@pytest.fixture
def mock_k8s_image() -> str:
    load_image(_MOCK_K8S_TARBALL)
    return _MOCK_K8S_IMAGE


@pytest.fixture
def client_image() -> str:
    load_image(_CLIENT_TARBALL)
    return _CLIENT_IMAGE


@pytest.fixture
def mock_k8s_server(mock_k8s_image: str, proxy_net: docker.models.networks.Network) -> Generator[MockK8sServer]:
    """Run mock k8s API as a container on proxy_net."""
    docker_client = docker.from_env()
    secrets_json = json.dumps(_FAKE_SECRETS)

    container = docker_client.containers.run(
        mock_k8s_image,
        command=[secrets_json, str(_MOCK_K8S_PORT)],
        name=f"mock-k8s-{os.getpid()}",
        network=proxy_net.name,
        detach=True,
    )

    try:
        # Use Docker DNS alias for the k8s server URL. mitmproxy reaches it
        # via container networking on proxy_net.
        assert proxy_net.name
        container_ip = _get_container_ip(container, proxy_net.name)
        logger.info("mock k8s API at %s:%d", container_ip, _MOCK_K8S_PORT)
        yield MockK8sServer(url=f"https://{container_ip}:{_MOCK_K8S_PORT}", container=container)
    finally:
        _save_output("mock-k8s-logs.log", container.logs().decode(errors="replace"))
        container.remove(force=True)


def test_k8s_secrets_via_egress_proxy_uds_mode(
    tmp_path: Path,
    mitmproxy_proxy: MitmproxyFixture,
    proxy_net: docker.models.networks.Network,
    mock_k8s_server: MockK8sServer,
    client_image: str,
) -> None:
    """read_k8s_secret succeeds through the egress proxy without a TCP auth proxy."""
    docker_client = docker.from_env()
    proxy_container = mitmproxy_proxy.container.get_wrapped_container()
    assert proxy_net.name
    proxy_ip = _get_container_ip(proxy_container, proxy_net.name)
    proxy_url = f"http://{_PROXY_CREDENTIALS}@{proxy_ip}:80"

    ca_path = tmp_path / "ca.pem"
    ca_path.write_bytes(mitmproxy_proxy.ca_cert_pem)

    container_name = f"k8s-proxy-test-client-{os.getpid()}"
    container = docker_client.containers.run(
        client_image,
        name=container_name,
        network=proxy_net.name,
        environment={"PROXY_URL": proxy_url, "K8S_SERVER": mock_k8s_server.url, "CA_FILE": "/certs/ca.pem"},
        volumes={str(ca_path): {"bind": "/certs/ca.pem", "mode": "ro"}},
        detach=True,
    )

    try:
        result = container.wait(timeout=120)
        stdout = container.logs(stdout=True, stderr=False).decode(errors="replace")
        stderr = container.logs(stdout=False, stderr=True).decode(errors="replace")
        _save_output("client-stdout.log", stdout)
        _save_output("client-stderr.log", stderr)
        mitm_logs = proxy_container.logs(stderr=True, stdout=False).decode(errors="replace")
        _save_output("mitmproxy-stderr.log", mitm_logs)

        exit_code = result.get("StatusCode", -1)
        assert exit_code == 0, f"Client container failed (rc={exit_code}):\nstderr:\n{stderr}\nstdout:\n{stdout}"

        output = json.loads(stdout.strip().split("\n")[-1])
        assert output["token"] == "fake-github-token"
    finally:
        container.remove(force=True)


if __name__ == "__main__":
    pytest_bazel.main()
