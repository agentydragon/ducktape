"""Shared mitmproxy testcontainer fixtures for proxy testing.

Provides a single `mitmproxy_proxy` fixture that starts mitmproxy on two Docker
networks (proxy net for internet access, isolated net for container-to-proxy
connectivity) and also publishes a host port. This setup works for both:

- Host-side tests (test_integration): access via `fixture.url` (127.0.0.1:port)
- Container E2E tests: access via `fixture.container_url` (DNS alias, no port)

Both `proxy_net` and `isolated_net` are separate yield fixtures with proper
teardown, available independently for tests that need them.
"""

import logging
import os
import shutil
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import docker
import docker.models.networks
import pytest
from testcontainers.core.container import DockerContainer

from devinfra.claude.testing.proxy_ca import generate_mock_ca
from third_party.containers.rlocations import MITMPROXY
from util.net import wait_for_port
from util.oci import load_oci_image
from util.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)

_PROXY_LISTEN_PORT = 80
_PROXY_CREDENTIALS = "proxy_user:test_jwt_token"

_HAR_CONTAINER_PATH = "/certs/proxy.har"

_MITMPROXY_CMD = [
    "mitmdump",
    "--listen-host",
    "0.0.0.0",
    "--listen-port",
    str(_PROXY_LISTEN_PORT),
    "--set",
    "confdir=/certs",
    "--set",
    f"proxyauth={_PROXY_CREDENTIALS}",
    "--ssl-insecure",
    "--set",
    f"hardump={_HAR_CONTAINER_PATH}",
]

# Network alias used for Docker DNS resolution on the isolated network
_MITMPROXY_ALIAS = "mitmproxy-proxy"


@dataclass(frozen=True)
class MitmproxyFixture:
    """Running mitmproxy container with proxy URLs and CA cert.

    url: host-accessible proxy URL (http://creds@127.0.0.1:port), for tests
         running on the host.
    container_url: container-accessible proxy URL via Docker DNS alias
         (http://creds@mitmproxy-proxy), no explicit port since mitmproxy
         listens on port 80 (HTTP default).
    """

    # TODO: test_integration runs the auth proxy on the host and needs url
    # (127.0.0.1:port). Consider converting to a container test so url can be
    # dropped and container_url is the only access path.
    url: str
    container_url: str
    ca_cert_pem: bytes
    container: DockerContainer


def _setup_mitmproxy_certs(tmp_path: Path) -> tuple[bytes, Path]:
    """Generate a mock CA and write mitmproxy confdir files.

    Returns (cert_pem, certs_dir). mitmproxy-ca.pem is key+cert concatenated
    (as mitmproxy expects); mitmproxy-ca-cert.pem is the cert only.
    """
    cert_pem, key_pem = generate_mock_ca()
    certs_dir = tmp_path / "mitmproxy_certs"
    certs_dir.mkdir()
    certs_dir.chmod(0o777)
    (certs_dir / "mitmproxy-ca.pem").write_bytes(key_pem + cert_pem)
    (certs_dir / "mitmproxy-ca-cert.pem").write_bytes(cert_pem)
    return cert_pem, certs_dir


def _save_mitmproxy_har(certs_dir: Path) -> None:
    """Copy the HAR dump from the host-mounted certs volume to test outputs.

    Called after container exit so mitmproxy has flushed the HAR file.
    """
    har_file = certs_dir / "proxy.har"
    if har_file.exists():
        out_dir = undeclared_outputs_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(har_file, out_dir / "proxy.har")


@pytest.fixture
def proxy_net() -> Generator[docker.models.networks.Network]:
    """Bridge Docker network with internet access (for the proxy container)."""
    net = docker.from_env().networks.create(f"e2e-proxy-{os.getpid()}", driver="bridge")
    try:
        yield net
    finally:
        net.remove()


@pytest.fixture
def isolated_net() -> Generator[docker.models.networks.Network]:
    """Internal bridge Docker network with no external routing (for the test container)."""
    net = docker.from_env().networks.create(f"e2e-isolated-{os.getpid()}", driver="bridge", internal=True)
    try:
        yield net
    finally:
        net.remove()


@pytest.fixture
def mitmproxy_proxy(
    tmp_path: Path, proxy_net: docker.models.networks.Network, isolated_net: docker.models.networks.Network
) -> Generator[MitmproxyFixture]:
    """Start a mitmproxy container for proxy testing.

    Starts on proxy_net only (not the default Docker bridge — isolated from
    unrelated traffic), with the alias _MITMPROXY_ALIAS on isolated_net for
    Docker DNS resolution. Also publishes a random host port for host-side tests.

    Yields MitmproxyFixture with:
    - url: http://creds@127.0.0.1:{host_port} (for tests running on the host)
    - container_url: http://creds@mitmproxy-proxy (for containers on isolated_net)
    """
    mitmproxy_image = load_oci_image(MITMPROXY)
    cert_pem, certs_dir = _setup_mitmproxy_certs(tmp_path)

    container = (
        DockerContainer(mitmproxy_image)
        .with_command(_MITMPROXY_CMD)
        .with_volume_mapping(str(certs_dir), "/certs", "rw")
        .with_exposed_ports(_PROXY_LISTEN_PORT)
        # Start on proxy_net only (not default bridge); name sets the container
        # hostname used for Docker DNS on any network it joins.
        .with_name(_MITMPROXY_ALIAS)
        .with_kwargs(network=proxy_net.name)
    )
    with container:
        isolated_net.connect(container.get_wrapped_container(), aliases=[_MITMPROXY_ALIAS])
        host_port = container.get_exposed_port(_PROXY_LISTEN_PORT)
        # TCP readiness gate — log-based waiting breaks with journald Docker log driver
        wait_for_port("127.0.0.1", int(host_port), timeout_secs=5)
        logger.info("mitmproxy ready: host=%s container=%s", host_port, _MITMPROXY_ALIAS)
        yield MitmproxyFixture(
            url=f"http://{_PROXY_CREDENTIALS}@127.0.0.1:{host_port}",
            container_url=f"http://{_PROXY_CREDENTIALS}@{_MITMPROXY_ALIAS}",
            ca_cert_pem=cert_pem,
            container=container,
        )
        # Stop mitmproxy gracefully so it flushes the HAR file to the host volume,
        # then copy it to test outputs before testcontainers removes the container.
        # 2s is enough for mitmdump to flush the HAR on SIGTERM.
        container.get_wrapped_container().stop(timeout=2)
        _save_mitmproxy_har(certs_dir)
