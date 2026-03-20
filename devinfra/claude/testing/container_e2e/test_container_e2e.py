"""Container E2E test: build wheel, install in container, run hook, bazel build through proxy.

This test verifies the full wheel packaging and session start flow in an isolated
Docker container with enforced network isolation (--internal Docker network prevents
all external connectivity).

Architecture:
    Host side:
        - Builds the ducktape wheel via Bazel
        - Builds MockEgressProxy OCI image via Bazel and loads it into Docker
        - Pulls e2e-container image from GHCR (python:3.13-slim + git + JDK)
        - Creates two Docker networks:
          - e2e-proxy (bridge): proxy container has internet access
          - e2e-isolated (internal bridge): test container <-> proxy container only
        - MockEgressProxy runs as a container on both networks
        - Drives test steps via docker exec calls

    Container side (via docker exec):
        - Installs ducktape wheel (pip through proxy -> MockEgressProxy container)
        - Runs claude-hook (session start hook) which sets up:
          auth proxy, supervisor, bazel wrapper, CA bundles, env file
        - Runs bazel build through the full proxy chain
"""

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
import aiohttp
import pytest
import pytest_bazel
import tenacity
from yarl import URL

from devinfra.claude.auth_proxy.setup import SSL_CA_ENV_VARS, SYSTEM_CA_BUNDLES
from devinfra.claude.auth_proxy.vars import PROXY_ENV_VARS
from devinfra.claude.testing.mock_egress_proxy import ConnectionStats, EgressProxyConfig
from util.bazel.runfiles import get_required_path
from util.oci import load_image
from util.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)

# Docker exec stream type codes (same as mcp_infra/exec/docker/container_session.py)
STREAM_TYPE_STDOUT = 1
STREAM_TYPE_STDERR = 2

# Rlocation for the ducktape wheel (built by //:wheel)
_WHEEL_RLOCATION = "_main/ducktape-0.1.0-py3-none-any.whl"

# Rlocation for a file in the test workspace (used to derive directory path)
_TEST_WORKSPACE_MODULE = "_main/devinfra/claude/testdata/test_workspace/MODULE.bazel"

# E2E test container image (pinned in MODULE.bazel via oci.pull, loaded via oci_load)
_E2E_IMAGE = "e2e-container:pinned"
_E2E_TARBALL = "_main/devinfra/claude/testing/container_e2e/e2e_container_load/tarball.tar"

# OCI image for the mock egress proxy container
_MOCK_PROXY_IMAGE = "mock-egress-proxy:latest"
_MOCK_PROXY_TARBALL = "_main/devinfra/claude/testing/mock_egress_proxy_load/tarball.tar"

# Container name prefix
_CONTAINER_NAME = "ducktape-container-e2e"

# Session ID used inside the container (determines log directory path)
_SESSION_ID = "container-e2e-test"

_ENV_FILE = f"/root/.claude/session-env/{_SESSION_ID}/sessionstart-hook-0.sh"

# Ports inside the proxy container
_PROXY_LISTEN_PORT = 8080
_PROXY_MGMT_PORT = 8081

# Timeout for proxy container readiness (seconds)
_PROXY_READY_TIMEOUT = 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _save_output(name: str, content: str) -> None:
    """Save content to undeclared test outputs."""
    out_dir = undeclared_outputs_dir() / "container-e2e"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_text(content)


def _cleanup_dangling_symlinks(directory: Path) -> None:
    """Remove dangling symlinks — Bazel rejects them in output trees."""
    for p in directory.rglob("*"):
        if p.is_symlink() and not p.exists():
            p.unlink()


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


async def _mgmt_get(session: aiohttp.ClientSession, url: URL) -> bytes:
    """HTTP GET against the proxy management API. Returns the response body."""
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.read()


@tenacity.retry(
    stop=tenacity.stop_after_delay(_PROXY_READY_TIMEOUT),
    wait=tenacity.wait_fixed(0.3),
    retry=tenacity.retry_if_exception_type((OSError, TimeoutError, aiohttp.ClientError)),
    reraise=True,
)
async def _wait_for_proxy_ready(session: aiohttp.ClientSession, mgmt_base: URL) -> None:
    """Poll the management /ready endpoint until it responds."""
    body = await _mgmt_get(session, mgmt_base / "ready")
    assert body == b"ok", f"Unexpected /ready response: {body!r}"


def _build_upstream_proxy_args(upstream: EgressProxyConfig, gateway_ip: str, proxy_shared: Path) -> list[str]:
    """Build CLI args for upstream proxy configuration.

    Rewrites localhost references to gateway_ip so the proxy container can
    reach host-side services via the bridge network gateway.
    """
    host = upstream.host
    if host in ("localhost", "127.0.0.1"):
        host = gateway_ip

    url = URL.build(scheme="http", user=upstream.username, password=upstream.password, host=host, port=upstream.port)

    args = ["--upstream-proxy-url", str(url)]

    if upstream.ca_bundle:
        shutil.copy2(upstream.ca_bundle, proxy_shared / "upstream_ca.pem")
        args += ["--upstream-ca-bundle", "/shared/upstream_ca.pem"]

    return args


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
    mgmt_base: URL
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
def mock_proxy_image() -> str:
    """Load the mock egress proxy OCI image into Docker."""
    load_image(_MOCK_PROXY_TARBALL)
    return _MOCK_PROXY_IMAGE


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
    tmp_path: Path, mock_proxy_image: str, docker_client: aiodocker.Docker, docker_networks: DockerNetworks
) -> AsyncGenerator[ProxySetup]:
    """Start MockEgressProxy container on the Docker networks.

    The proxy container sits on both the proxy network (internet access)
    and the isolated network (reachable by the test container). Yields a
    ProxySetup with the proxy URL, CA cert, management base URL, and isolated
    network name. Cleans up the proxy container on teardown.
    """
    proxy_shared = tmp_path / "proxy_shared"
    proxy_shared.mkdir()

    # Proxy logs land directly in undeclared test outputs
    proxy_logs_dir = undeclared_outputs_dir() / "container-e2e" / "proxy-logs"
    proxy_logs_dir.mkdir(parents=True, exist_ok=True)

    proxy_name = f"{_CONTAINER_NAME}-proxy-{os.getpid()}"

    proxy_cmd = [
        "--listen-port",
        str(_PROXY_LISTEN_PORT),
        "--mgmt-port",
        str(_PROXY_MGMT_PORT),
        "--username",
        "proxy_user",
        "--password",
        "test_jwt_token",
        "--log-dir",
        "/logs",
    ]

    upstream = EgressProxyConfig.from_env()
    binds_proxy: list[str] = [f"{proxy_logs_dir}:/logs"]
    if upstream:
        proxy_cmd += _build_upstream_proxy_args(upstream, docker_networks.gateway_ip, proxy_shared)
        if upstream.ca_bundle:
            binds_proxy.append(f"{proxy_shared / 'upstream_ca.pem'}:/shared/upstream_ca.pem:ro")
    else:
        proxy_cmd.append("--no-verify-target-certs")

    host_config: dict[str, Any] = {
        "NetworkMode": docker_networks.proxy_net_name,
        "PortBindings": {f"{_PROXY_MGMT_PORT}/tcp": [{"HostPort": "0"}]},
        "Binds": binds_proxy,
    }

    proxy_container = await docker_client.containers.create(
        {
            "Image": mock_proxy_image,
            "Cmd": proxy_cmd,
            "ExposedPorts": {f"{_PROXY_MGMT_PORT}/tcp": {}},
            "HostConfig": host_config,
        },
        name=proxy_name,
    )
    await proxy_container.start()
    logger.info("Started proxy container %s", proxy_name)

    try:
        # Get the published mgmt port on the host
        proxy_info = await proxy_container.show()
        mgmt_host_port = int(proxy_info["NetworkSettings"]["Ports"][f"{_PROXY_MGMT_PORT}/tcp"][0]["HostPort"])
        mgmt_base = URL.build(scheme="http", host="127.0.0.1", port=mgmt_host_port)
        logger.info("Proxy management API at %s", mgmt_base)

        # Connect proxy container to the isolated network
        await docker_networks.isolated_net.connect({"Container": proxy_container._id})

        async with aiohttp.ClientSession() as session:
            await _wait_for_proxy_ready(session, mgmt_base)
            logger.info("MockEgressProxy container is ready")

            mock_ca_pem = await _mgmt_get(session, mgmt_base / "ca.pem")

        # Get proxy container's IP on the isolated network (re-inspect after connect)
        proxy_info = await proxy_container.show()
        proxy_ip = proxy_info["NetworkSettings"]["Networks"][docker_networks.isolated_net_name]["IPAddress"]
        logger.info("Proxy container IP on isolated network: %s", proxy_ip)

        yield ProxySetup(
            proxy_url=f"http://proxy_user:test_jwt_token@{proxy_ip}:{_PROXY_LISTEN_PORT}",
            mock_ca_pem=mock_ca_pem,
            mgmt_base=mgmt_base,
            isolated_net_name=docker_networks.isolated_net_name,
        )

    finally:
        stdout = "".join(await proxy_container.log(stdout=True, stderr=True))
        _save_output("proxy-container.log", stdout)
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
    staged_wheel = staging / "ducktape-0.1.0-py3-none-any.whl"
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

    # Bind-mount the session dir so logs land directly in undeclared outputs
    session_logs_dir = undeclared_outputs_dir() / "container-e2e" / "session-logs"
    session_logs_dir.mkdir(parents=True, exist_ok=True)

    container_name = f"{_CONTAINER_NAME}-{os.getpid()}"

    env = {
        "CLAUDE_CODE_REMOTE": "true",
        "CLAUDE_PROJECT_DIR": "/project",
        "CLAUDE_ENV_FILE": _ENV_FILE,
        "DUCKTAPE_CLAUDE_HOOKS_INSTALL_BAZELISK": "true",
        "DUCKTAPE_CLAUDE_HOOKS_INSTALL_MKCERT": "false",
        "DUCKTAPE_CLAUDE_HOOKS_CONTAINER_RUNTIME": "none",
        "ANTHROPIC_CA_PATH": "/certs/mock_ca.pem",
        "WHEEL_PATH": "/wheel/ducktape-0.1.0-py3-none-any.whl",
    }
    for var in PROXY_ENV_VARS:
        env[var] = proxy_env.proxy_url
    for var in SSL_CA_ENV_VARS:
        env[var] = "/certs/combined_ca.pem"

    binds = [
        f"{staged_wheel}:/wheel/ducktape-0.1.0-py3-none-any.whl:ro",
        f"{mock_ca_path}:/certs/mock_ca.pem:ro",
        f"{combined_ca_path}:/certs/combined_ca.pem:ro",
        f"{staged_workspace}:/project/test_workspace:ro",
        f"{session_logs_dir}:/root/.claude/session-env/{_SESSION_ID}",
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
        await _exec(container, ["pip", "install", "--break-system-packages", "/wheel/ducktape-0.1.0-py3-none-any.whl"])

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

        # Fetch stats from management API
        async with aiohttp.ClientSession() as session:
            stats_body = await _mgmt_get(session, proxy_env.mgmt_base / "stats")
        stats = ConnectionStats.model_validate_json(stats_body)
        assert stats.total_connections > 0, (
            "Mock egress proxy received no connections - network isolation may not be working"
        )
        logger.info("Proxy stats: %s", stats)

    finally:
        stdout = "".join(await container.log(stdout=True, stderr=False))
        stderr = "".join(await container.log(stdout=False, stderr=True))
        _save_output("container-stdout.log", stdout)
        _save_output("container-stderr.log", stderr)

        # Fix permissions on bind-mounted session logs before container deletion.
        # The container runs as root, so files it creates (bazel cache, hook logs)
        # are owned by root. The CI runner can't read them, causing Bazel to fail
        # when collecting undeclared test outputs.
        await _exec(container, ["chmod", "-R", "a+rX", f"/root/.claude/session-env/{_SESSION_ID}"], check=False)

        await container.delete(force=True)

        # Remove container's bazel cache — not useful diagnostics and contains
        # symlinks into execroot that break Bazel's output collection.
        bazel_cache = session_logs_dir / "bazel-cache"
        if bazel_cache.exists():
            shutil.rmtree(bazel_cache, ignore_errors=True)

        _cleanup_dangling_symlinks(session_logs_dir)


if __name__ == "__main__":
    pytest_bazel.main()
