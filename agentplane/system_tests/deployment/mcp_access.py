"""Runtime access to the Dex-backed MCP fixtures in a k3s stack."""

import contextlib
import socket
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from agentplane.system_tests.deployment import rendered_fixture
from agentplane.system_tests.deployment.operator_tokens import dex_id_token
from agentplane.system_tests.driver.port_forward import PortForward
from agentplane.system_tests.k3s.baseline import K3s
from agentplane.testing import tls


@dataclass(frozen=True)
class Access:
    """The trust bundle, operator login, and standalone MCP endpoint exposed to a suite."""

    ca_bundle: Path
    operator_token: str
    operator_secret: str
    mcp_edge_url: str


def prepare_inputs(work: Path, fixtures: rendered_fixture.McpFixtures) -> rendered_fixture.McpInputs:
    """Prepare the Dex certificate and loopback origins that the fixture chart needs before apply."""
    ca = tls.make_ca("test-k3s-fixture-ca")
    cert, key = tls.issue_leaf(ca, fixtures.dex_host, work)
    # Free ports now; port-forwards listen once the app and MCP edge are Available.
    with socket.socket() as probe, socket.socket() as app_probe:
        probe.bind(("127.0.0.1", 0))
        app_probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        app_port = app_probe.getsockname()[1]
    return rendered_fixture.McpInputs(
        dex_ca_cert=ca.cert_pem.decode(),
        dex_cert=cert.read_text(),
        dex_key=key.read_text(),
        mcp_edge_origin=f"http://127.0.0.1:{port}",
        app_origin=f"http://127.0.0.1:{app_port}",
    )


@contextlib.contextmanager
def _port_forward(k3s: K3s, namespace: str, service: str, origin: str, port: int) -> Iterator[None]:
    """Forward the loopback `origin` to `service`'s `port` from when kubectl says it listens."""
    local = urlsplit(origin)
    if local.hostname is None or local.port is None:
        raise ValueError(f"port-forward origin needs a hostname and port: {origin!r}")
    with PortForward(
        (str(k3s.binary), "kubectl", "--kubeconfig", str(k3s.kubeconfig), "port-forward", "-n", namespace),
        resource=f"service/{service}",
        local_address=local.hostname,
        local_port=local.port,
        remote_port=port,
        readiness_timeout_s=30,
        output=sys.stdout.write,
    ):
        yield


@contextlib.contextmanager
def open_access(
    k3s: K3s,
    namespace: str,
    fixtures: rendered_fixture.McpFixtures,
    inputs: rendered_fixture.McpInputs,
    *,
    work: Path,
    system_roots: str,
) -> Iterator[Access]:
    """Own the MCP forwards while exposing the Dex trust bundle and operator token."""
    ca_bundle = work / "ca-certificates.crt"
    ca_bundle.write_text(system_roots + inputs.dex_ca_cert)
    with contextlib.ExitStack() as forwards:
        forwards.enter_context(
            _port_forward(k3s, namespace, fixtures.mcp_edge_service, inputs.mcp_edge_origin, fixtures.mcp_edge_port)
        )
        forwards.enter_context(
            _port_forward(k3s, namespace, fixtures.app_service, inputs.app_origin, fixtures.app_port)
        )
        token = dex_id_token(fixtures.operator_token_grant, ca_bundle=ca_bundle)
        yield Access(
            ca_bundle=ca_bundle,
            operator_token=token,
            operator_secret=fixtures.operator_secret,
            mcp_edge_url=f"{inputs.mcp_edge_origin}/mcp",
        )
