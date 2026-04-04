"""Container E2E test: build wheel, install in container, run hook, bazel build through proxy.

This test verifies the full wheel packaging and session start flow in an isolated
Docker container with enforced network isolation (--internal Docker network prevents
all external connectivity).

Architecture:
    Host side:
        - Builds the claude_hooks wheel via Bazel (baked into e2e_container image)
        - Loads mitmproxy:11 OCI image into Docker
        - Loads e2e-container image (python:3.13-slim + git + JDK + wheels, built by Bazel)
        - Creates two Docker networks:
          - e2e-proxy (bridge): proxy container has internet access
          - e2e-isolated (internal bridge): test container <-> proxy container only
        - mitmproxy runs as a container on both networks (mitmdump logs full
          URLs to stderr natively)
        - CA cert generated host-side and mounted into mitmproxy container
        - Drives test steps via docker exec calls

    Container side (via docker exec):
        - Installs claude_hooks wheel from /wheel/ (baked into image, deps fetched
          through proxy -> mitmproxy container)
        - Runs claude-hook (session start hook) which sets up:
          auth proxy, supervisor, bazel wrapper, CA bundles, env file
        - Runs bazel build through the full proxy chain

Network traffic profile (~110 MB with cli tools + apt skipped, measured 2026-03-21):
    releases.bazel.build:443        61 MB  56%  Bazel binary
    files.pythonhosted.org:443      27 MB  25%  pip wheel deps
    release-assets:443              18 MB  16%  Bazelisk + BCR module sources
    pypi.org:443                     4 MB   4%  pip index metadata
    bcr.bazel.build:443            0.2 MB  <1%  Bazel Central Registry metadata
    Skipped by settings: kubectl, apt packages, gh, flux.
    See proxy.har in undeclared outputs.
"""

import json
import logging
import os
import shlex
import shutil
from pathlib import Path

import docker
import docker.models.containers
import docker.models.networks
import pytest
import pytest_bazel

from devinfra.claude.auth_proxy.setup import SSL_CA_ENV_VARS, SYSTEM_CA_BUNDLES
from devinfra.claude.auth_proxy.vars import PROXY_ENV_VARS, get_upstream_proxy_url
from devinfra.claude.testing.mitmproxy_fixture import MitmproxyFixture
from util.bazel.runfiles import get_required_path
from util.oci import OciImage, load_oci_image
from util.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)

pytest_plugins = ["devinfra.claude.testing.mitmproxy_fixture"]

# Wheels and bazelisk are baked into the e2e_container image via pkg_tar layers
# (see BUILD.bazel). Wheels at /wheel/, bazelisk at /tools/bazelisk (on PATH).
_WHEEL_DIR = "/wheel"

# Rlocation for a file in the test workspace (used to derive directory path)
_TEST_WORKSPACE_MODULE = "_main/devinfra/claude/testdata/test_workspace/MODULE.bazel"

# E2E test container image (built by Bazel via rules_distroless, loaded via oci_image_info)
_E2E = OciImage(
    "_main/devinfra/claude/hook_daemon/session_start/container_e2e/e2e_container.rloc", "e2e-container:pinned"
)

# Container name prefix
_CONTAINER_NAME = "ducktape-container-e2e"

# Session ID used inside the container (determines log directory path)
_SESSION_ID = "container-e2e-test"

_ENV_FILE = f"/root/.claude/session-env/{_SESSION_ID}/sessionstart-hook-0.sh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _save_output(name: str, content: str) -> None:
    """Save content to undeclared test outputs."""
    out_dir = undeclared_outputs_dir() / "container-e2e"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_text(content)


def _exec(
    container: docker.models.containers.Container, cmd: list[str], *, workdir: str | None = None, check: bool = True
) -> tuple[int, bytes, bytes]:
    """Run a command in the container via docker exec.

    Returns (exit_code, stdout, stderr) as raw bytes. Raises AssertionError
    if check=True and the command fails.
    """
    kwargs: dict = {"stdout": True, "stderr": True, "demux": True}
    if workdir:
        kwargs["workdir"] = workdir

    exit_code, output = container.exec_run(cmd, **kwargs)
    stdout = output[0] or b""
    stderr = output[1] or b""

    logger.warning("exec %s -> rc=%d, stdout=%d bytes, stderr=%d bytes", cmd, exit_code, len(stdout), len(stderr))
    if stdout:
        logger.warning("stdout: %s", stdout.decode(errors="replace"))
    if stderr:
        logger.warning("stderr: %s", stderr.decode(errors="replace"))

    if check and exit_code != 0:
        raise AssertionError(
            f"Command {cmd} failed (rc={exit_code}):\n"
            f"stdout:\n{stdout.decode(errors='replace')}\n"
            f"stderr:\n{stderr.decode(errors='replace')}"
        )

    return exit_code, bytes(stdout), bytes(stderr)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_workspace_path() -> Path:
    """Resolve the test workspace directory from runfiles."""
    return get_required_path(_TEST_WORKSPACE_MODULE).parent


@pytest.fixture
def e2e_image() -> str:
    """Load the e2e container OCI image into Docker."""
    return load_oci_image(_E2E)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_container_e2e(
    tmp_path: Path,
    test_workspace_path: Path,
    mitmproxy_proxy: MitmproxyFixture,
    isolated_net: docker.models.networks.Network,
    e2e_image: str,
) -> None:
    """Full E2E: install wheel in container, run hook, bazel build through proxy."""
    assert not get_upstream_proxy_url(), (
        "Container E2E test requires direct internet access (no HTTPS_PROXY). "
        "Upstream proxy chaining is not supported — run via 'bazel test' on RBE."
    )

    # Copy files to a staging directory so Docker can mount real files
    # (runfiles may be symlinks that Docker cannot resolve in gVisor)
    staging = tmp_path / "staging"
    staging.mkdir()
    staged_workspace = staging / "test_workspace"
    shutil.copytree(test_workspace_path, staged_workspace)
    (staged_workspace / ".git").mkdir()  # pre-commit needs a git repo

    # Write CA certs to files for bind-mounting
    mock_ca_path = tmp_path / "mock_ca.pem"
    mock_ca_path.write_bytes(mitmproxy_proxy.ca_cert_pem)

    system_ca_path = next((p for p in SYSTEM_CA_BUNDLES if p.exists()), None)
    combined_ca_path = tmp_path / "combined_ca.pem"
    system_cas = system_ca_path.read_bytes() if system_ca_path else b""
    combined_ca_path.write_bytes(system_cas + b"\n" + mitmproxy_proxy.ca_cert_pem)

    container_name = f"{_CONTAINER_NAME}-{os.getpid()}"

    env = {
        "CLAUDE_CODE_REMOTE": "true",
        "CLAUDE_PROJECT_DIR": "/project",
        "CLAUDE_ENV_FILE": _ENV_FILE,
        "DUCKTAPE_CLAUDE_HOOKS_INSTALL_MKCERT": "false",
        "DUCKTAPE_CLAUDE_HOOKS_INSTALL_GH": "false",
        "DUCKTAPE_CLAUDE_HOOKS_INSTALL_KUBECTL": "false",
        "DUCKTAPE_CLAUDE_HOOKS_INSTALL_FLUX": "false",
        "DUCKTAPE_CLAUDE_HOOKS_INSTALL_APT_PACKAGES": "false",
        "DUCKTAPE_CLAUDE_HOOKS_CONTAINER_RUNTIME": "none",
        "ANTHROPIC_CA_PATH": "/certs/mock_ca.pem",
    }
    for var in PROXY_ENV_VARS:
        env[var] = mitmproxy_proxy.container_url
    for var in SSL_CA_ENV_VARS:
        env[var] = "/certs/combined_ca.pem"

    container = docker.from_env().containers.run(
        e2e_image,
        command=["sleep", "infinity"],
        name=container_name,
        environment=env,
        network=isolated_net.name,
        volumes={
            str(mock_ca_path): {"bind": "/certs/mock_ca.pem", "mode": "ro"},
            str(combined_ca_path): {"bind": "/certs/combined_ca.pem", "mode": "ro"},
            str(staged_workspace): {"bind": "/project", "mode": "ro"},
        },
        detach=True,
    )

    try:
        logger.info("Started test container %s on isolated network", container_name)

        # Verify network isolation — container must not reach the internet directly
        rc, _, _ = _exec(container, ["bash", "-c", "curl --max-time 3 https://google.com"], check=False)
        assert rc != 0, "Container should have no external internet access on --internal network"
        logger.info("Network isolation verified: container cannot reach internet directly")

        # Install claude_hooks wheel (baked into image at /wheel/).
        # Install local wheels by path to avoid PyPI name collision (a public
        # "claude-hooks" package exists on PyPI). Transitive deps are fetched
        # from PyPI via proxy.
        # TODO(container-e2e): Install via uv by reading .claude/settings.json
        # hook definition and piping the JSON into sh, instead of raw pip.
        logger.info("Installing wheel")
        _exec(container, ["ls", "-la", _WHEEL_DIR])
        _exec(
            container,
            [
                "pip",
                "install",
                "-v",
                "--break-system-packages",
                f"{_WHEEL_DIR}/ducktape_util-0.1.0-py3-none-any.whl",
                f"{_WHEEL_DIR}/claude_hooks-0.1.0-py3-none-any.whl",
            ],
        )
        _exec(container, ["which", "claude-hook"])

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
        _exec(container, ["bash", "-c", f"echo {shlex.quote(hook_input)} | claude-hook"])

        # Run bazel build through the proxy chain
        logger.info("Running bazel build")
        bazel_cmd = f"source {_ENV_FILE} && bazel build //:hello"
        _exec(container, ["bash", "-c", bazel_cmd], workdir="/project")

    finally:
        stdout_logs = container.logs(stdout=True, stderr=False)
        stderr_logs = container.logs(stdout=False, stderr=True)
        _save_output("container-stdout.log", stdout_logs.decode(errors="replace"))
        _save_output("container-stderr.log", stderr_logs.decode(errors="replace"))

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
            rc, content, _ = _exec(container, ["cat", f"{session_dir}/{log_file}"], check=False)
            if rc == 0:
                _save_output(log_file.replace("/", "-"), content.decode(errors="replace"))

        container.remove(force=True)


if __name__ == "__main__":
    pytest_bazel.main()
