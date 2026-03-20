"""Container E2E test: build wheel, install in container, run hook, bazel build through proxy.

This test verifies the full wheel packaging and session start flow in an isolated
Docker container with enforced network isolation (--internal Docker network prevents
all external connectivity).

Architecture:
    Host side:
        - Builds the claude_hooks wheel via Bazel
        - Loads mitmproxy:11 OCI image into Docker
        - Pulls e2e-container image from GHCR (python:3.13-slim + git + JDK)
        - Creates two Docker networks:
          - e2e-proxy (bridge): proxy container has internet access
          - e2e-isolated (internal bridge): test container <-> proxy container only
        - mitmproxy runs as a container on both networks (mitmdump logs full
          URLs to stderr natively)
        - CA cert generated host-side and mounted into mitmproxy container
        - Drives test steps via docker exec calls

    Container side (via docker exec):
        - Installs claude_hooks wheel (pip through proxy -> mitmproxy container)
        - Runs claude-hook (session start hook) which sets up:
          auth proxy, supervisor, bazel wrapper, CA bundles, env file
        - Runs bazel build through the full proxy chain

Network traffic profile (~221 MB with cli tools + apt skipped, measured 2026-03-20):
    releases.bazel.build:443       130 MB  59%  Bazel binary (2 conns)
    files.pythonhosted.org:443      81 MB  37%  pip wheel deps (protobuf, cryptography, etc.)
    pypi.org:443                    10 MB   4%  pip index metadata
    bcr.bazel.build:443            0.3 MB  <1%  Bazel Central Registry metadata
    Skipped by settings: kubectl (57 MB), apt packages (49 MB), gh, flux.
    See proxy.log in undeclared outputs.
"""

import asyncio
import json
import logging
import os
import shlex
import shutil
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiodocker
import pytest
import pytest_bazel
import tenacity
from yarl import URL

from devinfra.claude.auth_proxy.setup import SSL_CA_ENV_VARS, SYSTEM_CA_BUNDLES
from devinfra.claude.auth_proxy.vars import PROXY_ENV_VARS
from devinfra.claude.testing.mock_egress_proxy import EgressProxyConfig, generate_mock_ca
from util.bazel.runfiles import get_required_path
from util.oci import load_image
from util.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)

# Docker exec stream type codes (same as mcp_infra/exec/docker/container_session.py)
STREAM_TYPE_STDOUT = 1
STREAM_TYPE_STDERR = 2

# Rlocation for the claude_hooks wheel (built by //:claude_hooks_wheel)
_WHEEL_RLOCATION = "_main/claude_hooks-0.1.0-py3-none-any.whl"
_WHEEL_FILENAME = _WHEEL_RLOCATION.rsplit("/", 1)[-1]

# Rlocation for a file in the test workspace (used to derive directory path)
_TEST_WORKSPACE_MODULE = "_main/devinfra/claude/testdata/test_workspace/MODULE.bazel"

# E2E test container image (pinned in MODULE.bazel via oci.pull, loaded via oci_load)
_E2E_IMAGE = "e2e-container:pinned"
_E2E_TARBALL = "_main/devinfra/claude/testing/container_e2e/e2e_container_load/tarball.tar"

# mitmproxy OCI image for the proxy container
_MITMPROXY_IMAGE = "mitmproxy:11"
_MITMPROXY_TARBALL = "_main/devinfra/claude/testing/mitmproxy_load/tarball.tar"

# Container name prefix
_CONTAINER_NAME = "ducktape-container-e2e"

# Session ID used inside the container (determines log directory path)
_SESSION_ID = "container-e2e-test"

_ENV_FILE = f"/root/.claude/session-env/{_SESSION_ID}/sessionstart-hook-0.sh"

# Port inside the proxy container
_PROXY_LISTEN_PORT = 8080

# Proxy credentials
_PROXY_USERNAME = "proxy_user"
_PROXY_PASSWORD = "test_jwt_token"

# Timeout for proxy container readiness (seconds)
_PROXY_READY_TIMEOUT = 60

# Python-level test timeout — shorter than Bazel's so the finally block can
# collect logs before Bazel kills the process.
_TEST_TIMEOUT = 240


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _save_output(name: str, content: str) -> None:
    """Save content to undeclared test outputs."""
    out_dir = undeclared_outputs_dir() / "container-e2e"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_text(content)


async def _exec(
    container: aiodocker.containers.DockerContainer, cmd: list[str], *, workdir: str | None = None, check: bool = True
) -> tuple[int, bytes, bytes]:
    """Run a command in the container via docker exec.

    Returns (exit_code, stdout, stderr) as raw bytes. Raises AssertionError
    if check=True and the command fails.
    """
    exec_obj = await container.exec(cmd, stdout=True, stderr=True, stdin=False, tty=False, workdir=workdir or "")
    stream: Any = exec_obj.start()

    stdout_buf = bytearray()
    stderr_buf = bytearray()
    while True:
        chunk = await stream.read_out()
        if chunk is None:
            break
        data = chunk.data if isinstance(chunk.data, bytes) else chunk.data.encode()
        if chunk.stream == STREAM_TYPE_STDOUT:
            stdout_buf.extend(data)
        elif chunk.stream == STREAM_TYPE_STDERR:
            stderr_buf.extend(data)

    inspect_result = await exec_obj.inspect()
    exit_code = inspect_result.get("ExitCode", -1)

    logger.info("exec %s -> rc=%d, stdout=%d bytes, stderr=%d bytes", cmd, exit_code, len(stdout_buf), len(stderr_buf))
    if stdout_buf:
        logger.info("stdout: %s", stdout_buf.decode(errors="replace"))
    if stderr_buf:
        logger.info("stderr: %s", stderr_buf.decode(errors="replace"))

    if check and exit_code != 0:
        raise AssertionError(
            f"Command {cmd} failed (rc={exit_code}):\n"
            f"stdout:\n{stdout_buf.decode(errors='replace')}\n"
            f"stderr:\n{stderr_buf.decode(errors='replace')}"
        )

    return exit_code, bytes(stdout_buf), bytes(stderr_buf)


@tenacity.retry(
    stop=tenacity.stop_after_delay(_PROXY_READY_TIMEOUT),
    wait=tenacity.wait_fixed(0.3),
    retry=tenacity.retry_if_exception_type(OSError),
    reraise=True,
)
async def _wait_for_proxy_ready(host: str, port: int) -> None:
    """TCP connect to the proxy port until it accepts connections."""
    _, writer = await asyncio.open_connection(host, port)
    writer.close()
    await writer.wait_closed()


def _build_mitmproxy_cmd(upstream: EgressProxyConfig | None) -> list[str]:
    """Build mitmdump command line for the proxy container."""
    cmd = [
        "mitmdump",
        "--listen-host",
        "0.0.0.0",
        "--listen-port",
        str(_PROXY_LISTEN_PORT),
        "--set",
        "confdir=/certs",
        "--set",
        f"proxyauth={_PROXY_USERNAME}:{_PROXY_PASSWORD}",
    ]

    if upstream:
        url = URL.build(scheme="http", host=upstream.host, port=upstream.port)
        cmd += ["--mode", f"upstream:{url}"]
        if upstream.username and upstream.password:
            cmd += ["--upstream-auth", f"{upstream.username}:{upstream.password}"]
        if upstream.ca_bundle:
            cmd += ["--set", "ssl_verify_upstream_trusted_ca=/shared/upstream_ca.pem"]
        else:
            cmd += ["--ssl-insecure"]
    else:
        cmd += ["--ssl-insecure"]

    return cmd


# ---------------------------------------------------------------------------
# Fixture result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DockerNetworks:
    """Docker networks for the E2E test."""

    proxy_net_name: str
    isolated_net_name: str
    gateway_ip: str
    proxy_net: aiodocker.networks.DockerNetwork
    isolated_net: aiodocker.networks.DockerNetwork


@dataclass(frozen=True)
class ProxySetup:
    """Everything the test needs from the proxy infrastructure."""

    proxy_url: str
    mock_ca_pem: bytes
    isolated_net_name: str


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def wheel_path() -> Path:
    """Resolve the built ducktape wheel from runfiles."""
    return get_required_path(_WHEEL_RLOCATION)


@pytest.fixture
def test_workspace_path() -> Path:
    """Resolve the test workspace directory from runfiles."""
    return get_required_path(_TEST_WORKSPACE_MODULE).parent


@pytest.fixture
def e2e_image() -> str:
    """Load the e2e container OCI image into Docker."""
    load_image(_E2E_TARBALL)
    return _E2E_IMAGE


@pytest.fixture
def mitmproxy_image() -> str:
    """Load the mitmproxy OCI image into Docker."""
    load_image(_MITMPROXY_TARBALL)
    return _MITMPROXY_IMAGE


@pytest.fixture
async def docker_client() -> AsyncGenerator[aiodocker.Docker]:
    """Yield an aiodocker client, closing on teardown."""
    async with aiodocker.Docker() as client:
        yield client


@pytest.fixture
async def docker_networks(docker_client: aiodocker.Docker) -> AsyncGenerator[DockerNetworks]:
    """Create proxy (bridge) and isolated (internal) Docker networks.

    The proxy network provides internet access; the isolated network has no
    external routing. Cleaned up on teardown.
    """
    pid = os.getpid()
    proxy_net_name = f"e2e-proxy-{pid}"
    isolated_net_name = f"e2e-isolated-{pid}"

    proxy_net = await docker_client.networks.create({"Name": proxy_net_name, "Driver": "bridge"})
    isolated_net = await docker_client.networks.create(
        {"Name": isolated_net_name, "Driver": "bridge", "Internal": True}
    )

    proxy_net_info = await proxy_net.show()
    gateway_ip = proxy_net_info["IPAM"]["Config"][0]["Gateway"]
    logger.info("Created networks: proxy=%s (gateway %s), isolated=%s", proxy_net_name, gateway_ip, isolated_net_name)

    yield DockerNetworks(
        proxy_net_name=proxy_net_name,
        isolated_net_name=isolated_net_name,
        gateway_ip=gateway_ip,
        proxy_net=proxy_net,
        isolated_net=isolated_net,
    )

    await isolated_net.delete()
    await proxy_net.delete()


@pytest.fixture
async def proxy_env(
    tmp_path: Path, mitmproxy_image: str, docker_client: aiodocker.Docker, docker_networks: DockerNetworks
) -> AsyncGenerator[ProxySetup]:
    """Start mitmproxy container on the Docker networks.

    The proxy container sits on both the proxy network (internet access)
    and the isolated network (reachable by the test container). CA cert is
    generated host-side and mounted into the container. Yields a ProxySetup
    with the proxy URL, CA cert, and isolated network name.
    """
    # Generate CA host-side and write mitmproxy confdir files
    cert_pem, key_pem = generate_mock_ca()
    certs_dir = tmp_path / "mitmproxy_certs"
    certs_dir.mkdir()
    # mitmproxy expects key+cert concatenated as mitmproxy-ca.pem in confdir
    (certs_dir / "mitmproxy-ca.pem").write_bytes(key_pem + cert_pem)
    (certs_dir / "mitmproxy-ca-cert.pem").write_bytes(cert_pem)

    proxy_shared = tmp_path / "proxy_shared"
    proxy_shared.mkdir()

    proxy_name = f"{_CONTAINER_NAME}-proxy-{os.getpid()}"

    upstream = EgressProxyConfig.from_env()
    binds: list[str] = [f"{certs_dir}:/certs:ro"]
    if upstream:
        # Rewrite localhost to gateway IP so container can reach host services
        if upstream.host in ("localhost", "127.0.0.1"):
            upstream = EgressProxyConfig(
                host=docker_networks.gateway_ip,
                port=upstream.port,
                username=upstream.username,
                password=upstream.password,
                ca_bundle=upstream.ca_bundle,
            )
        if upstream.ca_bundle:
            shutil.copy2(upstream.ca_bundle, proxy_shared / "upstream_ca.pem")
            binds.append(f"{proxy_shared / 'upstream_ca.pem'}:/shared/upstream_ca.pem:ro")

    proxy_cmd = _build_mitmproxy_cmd(upstream)

    host_config: dict[str, Any] = {"NetworkMode": docker_networks.proxy_net_name, "Binds": binds}

    proxy_container = await docker_client.containers.create(
        {"Image": mitmproxy_image, "Cmd": proxy_cmd, "HostConfig": host_config}, name=proxy_name
    )
    await proxy_container.start()
    logger.info("Started mitmproxy container %s", proxy_name)

    try:
        # Connect proxy container to the isolated network
        await docker_networks.isolated_net.connect({"Container": proxy_container._id})

        # Wait for mitmproxy to be ready (TCP connect to proxy port)
        proxy_info = await proxy_container.show()
        proxy_ip = proxy_info["NetworkSettings"]["Networks"][docker_networks.isolated_net_name]["IPAddress"]
        logger.info("Proxy container IP on isolated network: %s", proxy_ip)

        await _wait_for_proxy_ready(proxy_ip, _PROXY_LISTEN_PORT)
        logger.info("mitmproxy container is ready")

        yield ProxySetup(
            proxy_url=f"http://{_PROXY_USERNAME}:{_PROXY_PASSWORD}@{proxy_ip}:{_PROXY_LISTEN_PORT}",
            mock_ca_pem=cert_pem,
            isolated_net_name=docker_networks.isolated_net_name,
        )

    finally:
        # Collect mitmproxy logs (stdout/stderr)
        proxy_logs = "".join(await proxy_container.log(stdout=True, stderr=True))
        _save_output("proxy.log", proxy_logs)

        await proxy_container.delete(force=True)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


async def test_container_e2e(
    tmp_path: Path,
    wheel_path: Path,
    test_workspace_path: Path,
    proxy_env: ProxySetup,
    docker_client: aiodocker.Docker,
    e2e_image: str,
) -> None:
    """Full E2E: install wheel in container, run hook, bazel build through proxy."""

    # Copy files to a staging directory so Docker can mount real files
    # (runfiles may be symlinks that Docker cannot resolve in gVisor)
    staging = tmp_path / "staging"
    staging.mkdir()
    staged_wheel = staging / _WHEEL_FILENAME
    shutil.copy2(wheel_path, staged_wheel)
    staged_workspace = staging / "test_workspace"
    shutil.copytree(test_workspace_path, staged_workspace)

    # Write CA certs to files for bind-mounting
    mock_ca_path = tmp_path / "mock_ca.pem"
    mock_ca_path.write_bytes(proxy_env.mock_ca_pem)

    system_ca_path = next((p for p in SYSTEM_CA_BUNDLES if p.exists()), None)
    combined_ca_path = tmp_path / "combined_ca.pem"
    system_cas = system_ca_path.read_bytes() if system_ca_path else b""
    combined_ca_path.write_bytes(system_cas + b"\n" + proxy_env.mock_ca_pem)

    container_name = f"{_CONTAINER_NAME}-{os.getpid()}"

    env = {
        "CLAUDE_CODE_REMOTE": "true",
        "CLAUDE_PROJECT_DIR": "/project",
        "CLAUDE_ENV_FILE": _ENV_FILE,
        "DUCKTAPE_CLAUDE_HOOKS_INSTALL_BAZELISK": "true",
        "DUCKTAPE_CLAUDE_HOOKS_INSTALL_MKCERT": "false",
        "DUCKTAPE_CLAUDE_HOOKS_INSTALL_GH": "false",
        "DUCKTAPE_CLAUDE_HOOKS_INSTALL_KUBECTL": "false",
        "DUCKTAPE_CLAUDE_HOOKS_INSTALL_FLUX": "false",
        "DUCKTAPE_CLAUDE_HOOKS_INSTALL_APT_PACKAGES": "false",
        "DUCKTAPE_CLAUDE_HOOKS_CONTAINER_RUNTIME": "none",
        "ANTHROPIC_CA_PATH": "/certs/mock_ca.pem",
        "WHEEL_PATH": f"/wheel/{_WHEEL_FILENAME}",
    }
    for var in PROXY_ENV_VARS:
        env[var] = proxy_env.proxy_url
    for var in SSL_CA_ENV_VARS:
        env[var] = "/certs/combined_ca.pem"

    binds = [
        f"{staged_wheel}:/wheel/{_WHEEL_FILENAME}:ro",
        f"{mock_ca_path}:/certs/mock_ca.pem:ro",
        f"{combined_ca_path}:/certs/combined_ca.pem:ro",
        f"{staged_workspace}:/project/test_workspace:ro",
    ]

    container = await docker_client.containers.create(
        {
            "Image": e2e_image,
            "Env": [f"{k}={v}" for k, v in env.items()],
            "Cmd": ["sleep", "infinity"],
            "HostConfig": {"NetworkMode": proxy_env.isolated_net_name, "Binds": binds},
        },
        name=container_name,
    )

    try:
        async with asyncio.timeout(_TEST_TIMEOUT):
            await container.start()
            logger.info("Started test container %s on isolated network", container_name)

            # Verify network isolation — container must not reach the internet directly
            rc, _, _ = await _exec(container, ["bash", "-c", "curl --max-time 3 https://google.com"], check=False)
            assert rc != 0, "Container should have no external internet access on --internal network"
            logger.info("Network isolation verified: container cannot reach internet directly")

            await _exec(container, ["mkdir", "-p", "/project/.git"])

            # Install ducktape wheel
            # TODO(container-e2e): Install via uv by reading .claude/settings.json
            # hook definition and piping the JSON into sh, instead of raw pip.
            logger.info("Installing wheel")
            await _exec(container, ["pip", "install", "--break-system-packages", f"/wheel/{_WHEEL_FILENAME}"])

            # Run session start hook
            logger.info("Running claude-hook (session start)")
            hook_input = json.dumps(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": _SESSION_ID,
                    "cwd": "/project",
                    "transcript_path": "/tmp/transcript.json",
                    "permission_mode": "default",
                    "source": "startup",
                    "model": "claude-sonnet-4-6",
                }
            )
            await _exec(container, ["bash", "-c", f"echo {shlex.quote(hook_input)} | claude-hook"])

            # Run bazel build through the proxy chain
            logger.info("Running bazel build")
            bazel_cmd = f"source {_ENV_FILE} && bazel build //:hello"
            await _exec(container, ["bash", "-c", bazel_cmd], workdir="/project/test_workspace")

    finally:
        stdout = "".join(await container.log(stdout=True, stderr=False))
        stderr = "".join(await container.log(stdout=False, stderr=True))
        _save_output("container-stdout.log", stdout)
        _save_output("container-stderr.log", stderr)

        # Extract specific log files from the container. We don't bind-mount
        # the session dir because the container (root) creates bazel cache/install
        # files that are unreadable by the CI runner and break Bazel's output collection.
        session_dir = f"/root/.claude/session-env/{_SESSION_ID}"
        for log_file in [
            "hook-daemon/daemon.log",
            "sessionstart-hook-0.sh",
            "supervisor/supervisord.log",
            "auth-proxy/bazelrc",
        ]:
            rc, content, _ = await _exec(container, ["cat", f"{session_dir}/{log_file}"], check=False)
            if rc == 0:
                _save_output(log_file.replace("/", "-"), content.decode(errors="replace"))

        await container.delete(force=True)


if __name__ == "__main__":
    pytest_bazel.main()
