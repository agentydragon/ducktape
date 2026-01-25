"""End-to-end tests for session start hook.

These tests run the full hook as Claude Code would invoke it,
with a ForwardingTLSProxy simulating Anthropic's TLS-inspecting proxy.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.parse
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_bazel

from net_util.net import pick_free_port
from runfiles import get_required_path
from tools.claude_hooks import settings
from tools.claude_hooks.proxy_vars import PROXY_ENV_VARS
from tools.claude_hooks.testing import runfiles_util, shell_helpers
from tools.claude_hooks.testing.forwarding_tls_proxy import ForwardingTLSProxy, UpstreamProxyConfig


@dataclass
class IsolatedDirs:
    """Isolated directories for e2e tests."""

    home: Path
    project: Path
    cache: Path
    config: Path
    env_file: Path


@pytest.fixture(scope="module")
def real_upstream_proxy() -> UpstreamProxyConfig | None:
    """Get upstream proxy for chaining in gVisor environments.

    After session_start hook runs, HTTPS_PROXY points to the local auth-forwarding
    proxy (B) which chains to Anthropic's proxy (A). ForwardingTLSProxy chains
    through B, which handles auth to A.

    Chain: ForwardingTLSProxy (A2) -> hook's bazel proxy (B) -> Anthropic proxy (A)
    """
    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy_url:
        return None
    parsed = urllib.parse.urlparse(proxy_url)
    if not parsed.hostname or not parsed.port:
        return None
    ca_bundle = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    return UpstreamProxyConfig(
        host=parsed.hostname,
        port=parsed.port,
        username=urllib.parse.unquote(parsed.username) if parsed.username else None,
        password=urllib.parse.unquote(parsed.password) if parsed.password else None,
        ca_bundle=ca_bundle,
    )


@pytest.fixture(scope="module")
def forwarding_proxy(real_upstream_proxy: UpstreamProxyConfig | None) -> Generator[ForwardingTLSProxy]:
    """Start a ForwardingTLSProxy that forwards to real internet with TLS MITM.

    Chains through the real upstream proxy (if available), allowing tests to work
    in environments like gVisor where direct internet access is blocked.
    """
    proxy = ForwardingTLSProxy(
        listen_port=0,  # Ephemeral port
        require_auth=True,
        username="proxy_user",
        password="test_jwt_token",
        upstream_proxy=real_upstream_proxy,
    )
    proxy.start()
    try:
        yield proxy
    finally:
        proxy.stop()


@pytest.fixture
def isolated_dirs(tmp_path: Path) -> IsolatedDirs:
    """Create isolated directories for the test."""
    dirs = IsolatedDirs(
        home=tmp_path / "home",
        project=tmp_path / "project",
        cache=tmp_path / "cache",
        config=tmp_path / "config",
        env_file=tmp_path / "env.sh",
    )
    dirs.home.mkdir()
    dirs.project.mkdir()
    dirs.cache.mkdir()
    dirs.config.mkdir()
    (dirs.project / ".git").mkdir()
    dirs.env_file.touch()
    return dirs


@pytest.fixture
def hook_env(isolated_dirs: IsolatedDirs, forwarding_proxy: ForwardingTLSProxy) -> dict[str, str]:
    """Set up environment for running the session start hook."""
    proxy_url = f"http://proxy_user:test_jwt_token@127.0.0.1:{forwarding_proxy.port}"

    # Pick isolated ports for supervisor and bazel proxy
    supervisor_port = pick_free_port()
    bazel_proxy_port = pick_free_port()

    use_wheel = os.environ.get(settings.ENV_USE_WHEEL) == "1"

    env = os.environ.copy()
    env.update(
        {
            # Required for web mode
            "CLAUDE_CODE_REMOTE": "true",
            "CLAUDE_PROJECT_DIR": str(isolated_dirs.project),
            "CLAUDE_ENV_FILE": str(isolated_dirs.env_file),
            # Isolated directories
            "HOME": str(isolated_dirs.home),
            "XDG_CACHE_HOME": str(isolated_dirs.cache),
            "XDG_CONFIG_HOME": str(isolated_dirs.config),
            # Isolated ports (avoid conflicts between tests)
            settings.ENV_SUPERVISOR_PORT: str(supervisor_port),
            settings.ENV_BAZEL_PROXY_PORT: str(bazel_proxy_port),
            # Disable nix installation (speeds up tests, avoids network)
            settings.ENV_SKIP_NIX: "1",
            # Disable podman setup (requires claude_hooks wheel install)
            settings.ENV_SKIP_PODMAN: "1",
            # Skip bazelisk download (tests use system bazel)
            settings.ENV_SKIP_BAZELISK: "1",
            # Proxy configuration (simulating Claude Code web)
            **dict.fromkeys(PROXY_ENV_VARS, proxy_url),
        }
    )

    # Ensure JAVA_HOME is passed through explicitly (needed for Java truststore)
    if java_home := os.environ.get("JAVA_HOME"):
        env["JAVA_HOME"] = java_home

    if not use_wheel:
        # Bazel test mode: use runfiles binaries
        env[settings.ENV_AUTH_PROXY_CMD] = str(get_required_path(runfiles_util.RUN_AUTH_PROXY))
    # When use_wheel=True, console scripts (claude-session-start, claude-auth-proxy) are in PATH
    # Note: bazel_wrapper auto-detects runfiles, no env var override needed

    return env


def make_hook_input(project_dir: Path, source: str = "startup") -> str:
    """Create JSON input that Claude Code would send to the hook."""
    return json.dumps(
        {
            "session_id": "test-session-123",
            "cwd": str(project_dir),
            "transcript_path": "/tmp/transcript.json",
            "permission_mode": "default",
            "hook_event_name": "SessionStart",
            "source": source,
        }
    )


def _cleanup_supervisor(config_dir: Path) -> None:
    """Kill any lingering supervisor processes."""
    pidfile = config_dir / "supervisor" / "supervisord.pid"
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
            # Send SIGTERM first
            os.kill(pid, signal.SIGTERM)
            # Wait for process to die (up to 2 seconds)
            for _ in range(20):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)  # Check if process exists
                except ProcessLookupError:
                    break  # Process is gone
            else:
                # Force kill if still running
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        except (ValueError, ProcessLookupError, OSError):
            pass
        # Clean up pidfile
        with contextlib.suppress(OSError):
            pidfile.unlink()


def run_session_start_hook(
    project_dir: Path, env: dict[str, str], source: str = "startup"
) -> subprocess.CompletedProcess[str]:
    """Run the session start hook with given environment.

    By default, runs via `python -m tools.claude_hooks.session_start` for Bazel tests.
    Set DUCKTAPE_CLAUDE_HOOKS_USE_WHEEL=1 to run via the installed `claude-session-start` console
    script instead - this tests the actual wheel packaging.

    Prints hook stdout/stderr for debugging (visible in CI logs on any test failure).
    """
    hook_input = make_hook_input(project_dir, source)

    if os.environ.get(settings.ENV_USE_WHEEL) == "1":
        # Run installed console script (tests wheel packaging)
        cmd = "claude-session-start"
    else:
        # Run via runfiles binary (Bazel test mode)
        cmd = get_required_path(runfiles_util.SESSION_START)

    result = subprocess.run([cmd], check=False, input=hook_input, capture_output=True, text=True, env=env, timeout=300)

    # Print hook output for debugging (pytest captures and shows on failure)
    print(f"\n=== Hook stdout ===\n{result.stdout}")
    print(f"\n=== Hook stderr ===\n{result.stderr}")

    return result


@pytest.fixture(autouse=True)
def cleanup_after_test(isolated_dirs: IsolatedDirs) -> Generator[None]:
    """Cleanup supervisor after each test."""
    yield
    # platformdirs respects XDG_CONFIG_HOME
    _cleanup_supervisor(isolated_dirs.config / "claude-hooks")


class TestFullSessionStartHook:
    """E2E tests running the complete session start hook."""

    @pytest.mark.skipif(not shutil.which("keytool"), reason="keytool required")
    def test_session_start_succeeds(self, isolated_dirs: IsolatedDirs, hook_env: dict[str, str]) -> None:
        """Run full session start hook and verify it succeeds."""
        # Skip bazelisk download - ForwardingTLSProxy doesn't handle cross-host redirects
        # (github.com -> objects.githubusercontent.com). Tests use system bazel instead.
        env = hook_env.copy()
        env[settings.ENV_SKIP_BAZELISK] = "1"
        result = run_session_start_hook(isolated_dirs.project, env)

        assert result.returncode == 0, f"Hook failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

        # Verify key artifacts created
        # platformdirs respects XDG_CACHE_HOME
        bazel_proxy_dir = isolated_dirs.cache / "claude-hooks" / "bazel-proxy"
        assert (bazel_proxy_dir / "bazelrc").exists(), "bazelrc not created"
        assert (bazel_proxy_dir / "anthropic_ca.pem").exists(), "CA not extracted"
        # Note: bazel wrapper is skipped in this test (DUCKTAPE_CLAUDE_HOOKS_SKIP_BAZELISK=1)

        # Verify supervisor started
        # platformdirs respects XDG_CONFIG_HOME
        supervisor_dir = isolated_dirs.config / "claude-hooks" / "supervisor"
        assert (supervisor_dir / "supervisord.pid").exists(), "supervisor not started"

    @pytest.mark.skipif(not shutil.which("keytool"), reason="keytool required")
    @pytest.mark.skipif(not shutil.which("bazel") and not shutil.which("bazelisk"), reason="bazel/bazelisk required")
    def test_bazel_build_after_hook(self, isolated_dirs: IsolatedDirs, hook_env: dict[str, str]) -> None:
        """Run hook, then verify bazel can build through the proxy."""
        # Skip bazelisk download - ForwardingTLSProxy doesn't handle cross-host redirects
        # Use system bazel instead (required via skipif above)
        env = hook_env.copy()
        env[settings.ENV_SKIP_BAZELISK] = "1"
        result = run_session_start_hook(isolated_dirs.project, env)
        assert result.returncode == 0, f"Hook failed: {result.stderr}"

        # Copy testdata workspace to test location
        # This is a minimal bzlmod workspace with no external dependencies, so the mock
        # ForwardingTLSProxy (which can't do real DNS/forwarding) isn't a blocker.
        test_file_dir = Path(__file__).parent
        testdata_workspace = test_file_dir / "testdata" / "test_workspace"
        workspace = isolated_dirs.project / "test_workspace"
        shutil.copytree(testdata_workspace, workspace)

        # Run bazel build in a shell that sources the env file (like Claude Code would)
        # The env file adds the wrapper dir to PATH, sets proxy vars to local auth-proxy,
        # and exports truststore configuration. The wrapper injects --bazelrc and falls
        # back to system bazel if bazelisk isn't installed.
        build_env = hook_env.copy()

        # Use shared helper to run bazel through env file (mimics Claude Code behavior)
        result = shell_helpers.run_with_env_file(
            command="bazel build //:hello",
            env_file=isolated_dirs.env_file,
            cwd=workspace,
            check=False,
            timeout=300,
            env=build_env,
        )
        assert result.returncode == 0, f"Bazel build failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

    @pytest.mark.skipif(not shutil.which("keytool"), reason="keytool required")
    def test_stale_socket_recovery(self, isolated_dirs: IsolatedDirs, hook_env: dict[str, str]) -> None:
        """Verify hook recovers from stale supervisor socket."""
        # Create stale socket/pidfile
        # platformdirs respects XDG_CONFIG_HOME
        supervisor_dir = isolated_dirs.config / "claude-hooks" / "supervisor"
        supervisor_dir.mkdir(parents=True, exist_ok=True)
        (supervisor_dir / "supervisor.sock").touch()
        (supervisor_dir / "supervisord.pid").write_text("99999")  # Non-existent PID

        # Skip bazelisk download - this test focuses on stale socket recovery
        # and ForwardingTLSProxy doesn't handle cross-host redirects (github -> objects.githubusercontent)
        env = hook_env.copy()
        env[settings.ENV_SKIP_BAZELISK] = "1"
        result = run_session_start_hook(isolated_dirs.project, env)

        assert result.returncode == 0, f"Hook failed with stale socket:\nstderr: {result.stderr}"

    @pytest.mark.skipif(not shutil.which("keytool"), reason="keytool required")
    def test_resume_event(self, isolated_dirs: IsolatedDirs, hook_env: dict[str, str]) -> None:
        """Test that resume events also work correctly."""
        # Skip bazelisk download - this test focuses on resume event handling
        # and ForwardingTLSProxy doesn't handle cross-host redirects (github -> objects.githubusercontent)
        env = hook_env.copy()
        env[settings.ENV_SKIP_BAZELISK] = "1"
        result = run_session_start_hook(isolated_dirs.project, env, source="resume")

        assert result.returncode == 0, f"Hook failed on resume:\nstderr: {result.stderr}"


def _can_use_podman() -> bool:
    """Check if podman is available for use.

    Returns True if podman is already installed.
    Installing via apt-get requires root, which we want to avoid.
    """
    return bool(shutil.which("podman"))


def _extract_docker_host_socket(env_file: Path) -> Path:
    """Extract socket path from DOCKER_HOST in env file.

    The env file contains export statements like:
        export DOCKER_HOST="unix:///tmp/claude-podman-abc123.sock"
    """
    env_content = env_file.read_text()
    assert "DOCKER_HOST" in env_content, "DOCKER_HOST not set in env file"

    match = re.search(r'DOCKER_HOST="?unix://([^"\s]+)"?', env_content)
    assert match, f"Could not extract DOCKER_HOST socket path from env file:\n{env_content}"
    return Path(match.group(1))


class TestPodmanIntegration:
    """E2E tests for podman integration with session start hook.

    These tests verify that podman is properly configured and can run containers
    after the session start hook runs. Config and socket use isolated paths
    (~/.cache/claude-hooks/podman/).
    """

    @pytest.fixture
    def podman_hook_env(self, isolated_dirs: IsolatedDirs, forwarding_proxy: ForwardingTLSProxy) -> dict[str, str]:
        """Set up environment for running session start hook WITH podman enabled."""
        proxy_url = f"http://proxy_user:test_jwt_token@127.0.0.1:{forwarding_proxy.port}"

        # Pick isolated ports for supervisor and bazel proxy
        supervisor_port = pick_free_port()
        bazel_proxy_port = pick_free_port()

        use_wheel = os.environ.get(settings.ENV_USE_WHEEL) == "1"

        env = os.environ.copy()
        env.update(
            {
                # Required for web mode
                "CLAUDE_CODE_REMOTE": "true",
                "CLAUDE_PROJECT_DIR": str(isolated_dirs.project),
                "CLAUDE_ENV_FILE": str(isolated_dirs.env_file),
                # Proxy configuration (simulating Claude Code web)
                "https_proxy": proxy_url,
                "HTTPS_PROXY": proxy_url,
                "http_proxy": proxy_url,
                "HTTP_PROXY": proxy_url,
                # Isolated directories
                "HOME": str(isolated_dirs.home),
                "XDG_CACHE_HOME": str(isolated_dirs.cache),
                "XDG_CONFIG_HOME": str(isolated_dirs.config),
                # Isolated ports (avoid conflicts between tests)
                settings.ENV_SUPERVISOR_PORT: str(supervisor_port),
                settings.ENV_BAZEL_PROXY_PORT: str(bazel_proxy_port),
                # Disable nix and bazelisk (speeds up tests)
                settings.ENV_SKIP_NIX: "1",
                settings.ENV_SKIP_BAZELISK: "1",
                # NOTE: NOT setting ENV_SKIP_PODMAN - podman is enabled
            }
        )

        # Ensure JAVA_HOME is passed through explicitly (needed for Java truststore)
        if java_home := os.environ.get("JAVA_HOME"):
            env["JAVA_HOME"] = java_home

        if not use_wheel:
            # Bazel test mode: use runfiles binaries
            env[settings.ENV_AUTH_PROXY_CMD] = str(get_required_path(runfiles_util.RUN_AUTH_PROXY))

        return env

    @pytest.mark.skipif(not shutil.which("keytool"), reason="keytool required")
    @pytest.mark.skipif(not _can_use_podman(), reason="podman not installed")
    def test_podman_service_starts(self, isolated_dirs: IsolatedDirs, podman_hook_env: dict[str, str]) -> None:
        """Verify podman service starts after session start hook."""
        result = run_session_start_hook(isolated_dirs.project, podman_hook_env)

        assert result.returncode == 0, "Hook failed with non-zero exit code"

        socket_path = _extract_docker_host_socket(isolated_dirs.env_file)
        assert socket_path.exists(), f"Podman socket not created at {socket_path}"

    @pytest.mark.skipif(not shutil.which("keytool"), reason="keytool required")
    @pytest.mark.skipif(not _can_use_podman(), reason="podman not installed")
    def test_podman_can_run_container(
        self, isolated_dirs: IsolatedDirs, podman_hook_env: dict[str, str], forwarding_proxy: ForwardingTLSProxy
    ) -> None:
        """Verify podman can run a container after session start hook.

        Runs podman through the ForwardingTLSProxy to verify the full proxy chain works,
        including CA certificate configuration for container registry pulls.
        """
        result = run_session_start_hook(isolated_dirs.project, podman_hook_env)

        assert result.returncode == 0, "Hook failed with non-zero exit code"

        socket_path = _extract_docker_host_socket(isolated_dirs.env_file)
        assert socket_path.exists(), f"Podman socket not created at {socket_path}"

        # Verify we can run podman hello-world through the proxy
        # The gVisor annotation is auto-applied via containers.conf
        # Run through env file to pick up SSL_CERT_FILE for TLS proxy CA
        podman_result = shell_helpers.run_with_env_file(
            command="podman run --rm docker.io/library/hello-world",
            env_file=isolated_dirs.env_file,
            cwd=isolated_dirs.project,
            check=False,
            timeout=120,
            env=podman_hook_env,
        )

        # Include proxy stats in failure message for debugging
        proxy_stats = (
            f"\nProxy stats: {forwarding_proxy.stats.total_connections} total, "
            f"{forwarding_proxy.stats.successful_connections} success, "
            f"{forwarding_proxy.stats.failed_connections} failed"
        )
        if forwarding_proxy.stats.errors:
            proxy_stats += f"\nProxy errors: {forwarding_proxy.stats.errors[-5:]}"

        assert podman_result.returncode == 0, (
            f"Podman run failed:\nstdout: {podman_result.stdout}\nstderr: {podman_result.stderr}{proxy_stats}"
        )
        assert "Hello from Docker" in podman_result.stdout, (
            f"Expected 'Hello from Docker' in output:\n{podman_result.stdout}{proxy_stats}"
        )


class TestForwardingProxy:
    """Tests for the ForwardingTLSProxy itself."""

    def test_proxy_starts_and_stops(self) -> None:
        """Test basic proxy lifecycle."""
        proxy = ForwardingTLSProxy(listen_port=0, require_auth=False)
        proxy.start()
        assert proxy.port > 0
        assert proxy.ca_cert_pem
        proxy.stop()

    def test_proxy_requires_auth(self, forwarding_proxy: ForwardingTLSProxy) -> None:
        """Test that proxy rejects unauthenticated requests."""
        sock = socket.create_connection(("127.0.0.1", forwarding_proxy.port), timeout=5)
        try:
            # Send CONNECT without auth
            sock.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
            response = sock.recv(1024)
            assert b"407" in response, f"Expected 407, got: {response!r}"
        finally:
            sock.close()

    def test_proxy_accepts_auth(self, forwarding_proxy: ForwardingTLSProxy) -> None:
        """Test that proxy accepts valid authentication."""
        sock = socket.create_connection(("127.0.0.1", forwarding_proxy.port), timeout=5)
        try:
            # Send CONNECT with auth
            creds = base64.b64encode(b"proxy_user:test_jwt_token").decode()
            request = f"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\nProxy-Authorization: Basic {creds}\r\n\r\n"
            sock.sendall(request.encode())
            response = sock.recv(1024)
            assert b"200" in response, f"Expected 200, got: {response!r}"
        finally:
            sock.close()


if __name__ == "__main__":
    pytest_bazel.main()
