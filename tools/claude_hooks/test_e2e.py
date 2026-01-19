"""End-to-end tests for session start hook.

These tests run the full hook as Claude Code would invoke it,
with a ForwardingTLSProxy simulating Anthropic's TLS-inspecting proxy.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_bazel

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


@pytest.fixture
def hook_env(isolated_dirs: IsolatedDirs, forwarding_proxy: ForwardingTLSProxy) -> dict[str, str]:
    """Set up environment for running the session start hook."""
    proxy_url = f"http://proxy_user:test_jwt_token@127.0.0.1:{forwarding_proxy.port}"

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
            # Disable nix installation (speeds up tests, avoids network)
            "CLAUDE_HOOKS_SKIP_NIX": "1",
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
            os.kill(pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError, OSError):
            pass


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
    _cleanup_supervisor(isolated_dirs.config)


class TestFullSessionStartHook:
    """E2E tests running the complete session start hook."""

    @pytest.mark.skipif(not shutil.which("keytool"), reason="keytool required")
    def test_session_start_succeeds(self, isolated_dirs: IsolatedDirs, hook_env: dict[str, str]) -> None:
        """Run full session start hook and verify it succeeds."""
        result = run_session_start_hook(isolated_dirs.project, hook_env)

        assert result.returncode == 0, f"Hook failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

        # Verify key artifacts created
        cache = isolated_dirs.cache / "bazel-proxy"
        assert (cache / "bazelrc").exists(), "bazelrc not created"
        assert (cache / "anthropic_ca.pem").exists(), "CA not extracted"
        assert (cache / "bin" / "bazel").exists(), "bazel wrapper not created"

        # Verify supervisor started
        config = isolated_dirs.config / "supervisor"
        assert (config / "supervisord.pid").exists(), "supervisor not started"

    @pytest.mark.skipif(not shutil.which("keytool"), reason="keytool required")
    @pytest.mark.skipif(not shutil.which("bazel") and not shutil.which("bazelisk"), reason="bazel/bazelisk required")
    def test_bazel_build_after_hook(self, isolated_dirs: IsolatedDirs, hook_env: dict[str, str]) -> None:
        """Run hook, then verify bazel can build through the proxy."""
        result = run_session_start_hook(isolated_dirs.project, hook_env)
        assert result.returncode == 0, f"Hook failed: {result.stderr}"

        # Create test workspace
        workspace = isolated_dirs.project
        (workspace / "MODULE.bazel").write_text(
            """
module(name = "test")
bazel_dep(name = "rules_python", version = "0.27.1")
"""
        )
        (workspace / "BUILD.bazel").write_text(
            """
genrule(
    name = "hello",
    outs = ["hello.txt"],
    cmd = "echo hello > $@",
)
"""
        )

        # Run bazel build with the configured environment
        cache = isolated_dirs.cache / "bazel-proxy"
        build_env = hook_env.copy()
        build_env["PATH"] = f"{cache / 'bin'}:{build_env.get('PATH', '')}"
        build_env["BAZEL_SYSTEM_BAZELRC_PATH"] = str(cache / "bazelrc")

        result = subprocess.run(
            ["bazel", "build", "//:hello"],
            check=False,
            cwd=workspace,
            capture_output=True,
            text=True,
            env=build_env,
            timeout=300,
        )
        assert result.returncode == 0, f"Bazel build failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

    @pytest.mark.skipif(not shutil.which("keytool"), reason="keytool required")
    def test_stale_socket_recovery(self, isolated_dirs: IsolatedDirs, hook_env: dict[str, str]) -> None:
        """Verify hook recovers from stale supervisor socket."""
        # Create stale socket/pidfile
        config = isolated_dirs.config / "supervisor"
        config.mkdir(parents=True, exist_ok=True)
        (config / "supervisor.sock").touch()
        (config / "supervisord.pid").write_text("99999")  # Non-existent PID

        result = run_session_start_hook(isolated_dirs.project, hook_env)

        assert result.returncode == 0, f"Hook failed with stale socket:\nstderr: {result.stderr}"

    @pytest.mark.skipif(not shutil.which("keytool"), reason="keytool required")
    def test_resume_event(self, isolated_dirs: IsolatedDirs, hook_env: dict[str, str]) -> None:
        """Test that resume events also work correctly."""
        result = run_session_start_hook(isolated_dirs.project, hook_env, source="resume")

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
    sys.exit(pytest.main([__file__, "-v"]))

if __name__ == "__main__":
    pytest_bazel.main()
