"""End-to-end tests for session start hook.

These tests run the full hook as Claude Code would invoke it,
with a ForwardingTLSProxy simulating Anthropic's TLS-inspecting proxy.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_bazel

from tools.claude_hooks.testing import shell_helpers
from tools.claude_hooks.testing.forwarding_tls_proxy import ForwardingTLSProxy


@dataclass
class IsolatedDirs:
    """Isolated directories for e2e tests."""

    home: Path
    project: Path
    cache: Path
    config: Path
    env_file: Path


@pytest.fixture(scope="module")
def forwarding_proxy() -> Generator[ForwardingTLSProxy]:
    """Start a ForwardingTLSProxy that forwards to real internet with TLS MITM."""
    proxy = ForwardingTLSProxy(
        listen_port=0,  # Ephemeral port
        require_auth=True,
        username="proxy_user",
        password="test_jwt_token",
    )
    proxy.start()
    yield proxy
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


def _pick_free_port() -> int:
    """Pick an available ephemeral port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port: int = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def hook_env(isolated_dirs: IsolatedDirs, forwarding_proxy: ForwardingTLSProxy) -> dict[str, str]:
    """Set up environment for running the session start hook."""
    proxy_url = f"http://proxy_user:test_jwt_token@127.0.0.1:{forwarding_proxy.port}"

    # Pick isolated ports for supervisor and bazel proxy
    supervisor_port = _pick_free_port()
    bazel_proxy_port = _pick_free_port()

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
            "CLAUDE_HOOKS_SUPERVISOR_PORT": str(supervisor_port),
            "CLAUDE_HOOKS_BAZEL_PROXY_PORT": str(bazel_proxy_port),
            # PYTHONPATH for subprocess to find modules (Bazel doesn't export this)
            "PYTHONPATH": os.pathsep.join(sys.path),
            # Disable nix installation (speeds up tests, avoids network)
            "CLAUDE_HOOKS_SKIP_NIX": "1",
            # Disable podman setup (requires claude_hooks wheel install)
            "CLAUDE_HOOKS_SKIP_PODMAN": "1",
            # Use python -m for proxy in tests (console script not available in Bazel)
            "CLAUDE_AUTH_PROXY_CMD": f"{sys.executable} -m tools.claude_hooks.proxy.run_auth_proxy",
        }
    )
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
    """Run the session start hook with given environment."""
    hook_input = make_hook_input(project_dir, source)
    return subprocess.run(
        [sys.executable, "-m", "tools.claude_hooks.session_start"],
        check=False,
        input=hook_input,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )


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
        env["CLAUDE_HOOKS_SKIP_BAZELISK"] = "1"
        result = run_session_start_hook(isolated_dirs.project, env)

        assert result.returncode == 0, f"Hook failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

        # Verify key artifacts created
        # platformdirs respects XDG_CACHE_HOME
        bazel_proxy_dir = isolated_dirs.cache / "claude-hooks" / "bazel-proxy"
        assert (bazel_proxy_dir / "bazelrc").exists(), "bazelrc not created"
        assert (bazel_proxy_dir / "anthropic_ca.pem").exists(), "CA not extracted"
        # Note: bazel wrapper is skipped in this test (CLAUDE_HOOKS_SKIP_BAZELISK=1)

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
        env["CLAUDE_HOOKS_SKIP_BAZELISK"] = "1"
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
        # platformdirs respects XDG_CACHE_HOME
        bazel_proxy_dir = isolated_dirs.cache / "claude-hooks" / "bazel-proxy"
        build_env = hook_env.copy()
        build_env["BAZEL_SYSTEM_BAZELRC_PATH"] = str(bazel_proxy_dir / "bazelrc")

        # Remove direct proxy env vars - bazel should use the local auth-forwarding proxy
        # configured in bazelrc, not the ForwardingTLSProxy which requires auth
        for var in ["https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"]:
            build_env.pop(var, None)

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
        env["CLAUDE_HOOKS_SKIP_BAZELISK"] = "1"
        result = run_session_start_hook(isolated_dirs.project, env)

        assert result.returncode == 0, f"Hook failed with stale socket:\nstderr: {result.stderr}"

    @pytest.mark.skipif(not shutil.which("keytool"), reason="keytool required")
    def test_resume_event(self, isolated_dirs: IsolatedDirs, hook_env: dict[str, str]) -> None:
        """Test that resume events also work correctly."""
        # Skip bazelisk download - this test focuses on resume event handling
        # and ForwardingTLSProxy doesn't handle cross-host redirects (github -> objects.githubusercontent)
        env = hook_env.copy()
        env["CLAUDE_HOOKS_SKIP_BAZELISK"] = "1"
        result = run_session_start_hook(isolated_dirs.project, env, source="resume")

        assert result.returncode == 0, f"Hook failed on resume:\nstderr: {result.stderr}"


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
