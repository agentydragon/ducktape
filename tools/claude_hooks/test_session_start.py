"""Tests for session_start hook.

Includes:
- HookInput parsing tests (unit)
- Full hook subprocess tests (e2e) with MockAnthropicProxy simulating Anthropic's proxy
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_bazel

from net_util.net import pick_free_port
from runfiles import get_required_path
from tools.claude_hooks import settings
from tools.claude_hooks.proxy_setup import SSL_CA_ENV_VARS, SYSTEM_CA_BUNDLES
from tools.claude_hooks.proxy_vars import PROXY_ENV_VARS
from tools.claude_hooks.session_start import HookInput, HookSource
from tools.claude_hooks.testing import runfiles_util, shell_helpers
from tools.claude_hooks.testing.fixtures import MockProxyFixture
from tools.claude_hooks.testing.mock_anthropic_proxy import MockAnthropicProxy

# Register fixtures from module (pytest-native, no direct name import needed)
pytest_plugins = ["tools.claude_hooks.testing.fixtures"]

# === HookInput parsing tests ===


def test_hook_input_without_permission_mode() -> None:
    """Validate HookInput accepts missing permission_mode.

    Claude Code Web was observed (2025-01-18) not sending permission_mode
    for SessionStart:resume events, despite documentation claiming it's required.
    """
    data = {
        "session_id": "test-session",
        "cwd": "/tmp",
        "transcript_path": "/tmp/transcript.json",
        "hook_event_name": "SessionStart",
        "source": "resume",
        # Note: permission_mode intentionally omitted
    }
    result = HookInput.model_validate(data)
    assert result.permission_mode == "default"


def test_hook_input_with_permission_mode() -> None:
    """Validate HookInput accepts explicit permission_mode."""
    data = {
        "session_id": "test-session",
        "cwd": "/tmp",
        "transcript_path": "/tmp/transcript.json",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "permission_mode": "plan",
    }
    result = HookInput.model_validate(data)
    assert result.permission_mode == "plan"


@pytest.mark.parametrize("permission_mode", ["default", "plan", "acceptEdits", "dontAsk", "bypassPermissions"])
def test_hook_input_all_permission_modes(permission_mode: str) -> None:
    """Validate HookInput accepts all documented permission_mode values."""
    data = {
        "session_id": "test-session",
        "cwd": "/tmp",
        "transcript_path": "/tmp/transcript.json",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "permission_mode": permission_mode,
    }
    result = HookInput.model_validate(data)
    assert result.permission_mode == permission_mode


# === E2E subprocess tests ===


@dataclass
class IsolatedDirs:
    """Isolated directories for e2e tests."""

    home: Path
    project: Path
    cache: Path
    config: Path
    env_file: Path


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
def system_bazel() -> str:
    """Get system bazel/bazelisk path, failing if not found."""
    path = shutil.which("bazelisk") or shutil.which("bazel")
    if not path:
        pytest.fail("Neither bazelisk nor bazel found on PATH")
    return path


def _setup_hook_env(
    monkeypatch: pytest.MonkeyPatch,
    isolated_dirs: IsolatedDirs,
    mock_proxy: MockAnthropicProxy,
    system_bazel: str,
    *,
    skip_podman: bool = True,
) -> None:
    """Set up environment variables for running session start hook via monkeypatch.

    Args:
        monkeypatch: pytest monkeypatch fixture
        isolated_dirs: Test isolation directories
        mock_proxy: TLS proxy simulating Anthropic's proxy
        system_bazel: Path to system bazel/bazelisk
        skip_podman: Whether to skip podman setup (default True)
    """
    # Create combined CA bundle with system CAs + mock proxy CA
    # This allows bazelisk and other TLS clients to trust the mock proxy
    system_ca_path = next((p for p in SYSTEM_CA_BUNDLES if p.exists()), None)
    combined_ca_path = isolated_dirs.cache / "combined_ca.pem"
    system_cas = system_ca_path.read_bytes() if system_ca_path else b""
    combined_ca_path.write_bytes(system_cas + b"\n" + mock_proxy.ca_cert_pem)

    # Pick isolated ports for supervisor and bazel proxy
    supervisor_port = pick_free_port()
    bazel_proxy_port = pick_free_port()

    # Required for web mode
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(isolated_dirs.project))
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(isolated_dirs.env_file))

    # Isolated directories
    monkeypatch.setenv("HOME", str(isolated_dirs.home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(isolated_dirs.cache))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated_dirs.config))

    # Isolated ports (avoid conflicts between tests)
    monkeypatch.setenv(settings.ENV_SUPERVISOR_PORT, str(supervisor_port))
    monkeypatch.setenv(settings.ENV_BAZEL_PROXY_PORT, str(bazel_proxy_port))

    # Disable nix and bazelisk (speeds up tests)
    monkeypatch.setenv(settings.ENV_SKIP_NIX, "1")
    monkeypatch.setenv(settings.ENV_SKIP_BAZELISK, "1")

    # Provide system bazel path (required when skip_bazelisk=True)
    monkeypatch.setenv(settings.ENV_SYSTEM_BAZEL, system_bazel)

    # Proxy configuration (simulating Claude Code web)
    for var in PROXY_ENV_VARS:
        monkeypatch.setenv(var, mock_proxy.url)

    # Configure SSL trust for the mock proxy's CA
    # This is needed for bazelisk and other tools to trust TLS connections through the mock proxy
    for var in SSL_CA_ENV_VARS:
        monkeypatch.setenv(var, str(combined_ca_path))

    if skip_podman:
        monkeypatch.setenv(settings.ENV_SKIP_PODMAN, "1")

    # Bazel test mode: use runfiles binaries (unless testing wheel)
    if os.environ.get(settings.ENV_USE_WHEEL) != "1":
        monkeypatch.setenv(settings.ENV_AUTH_PROXY_CMD, str(get_required_path(runfiles_util.RUN_AUTH_PROXY)))


@pytest.fixture
def hook_env(
    monkeypatch: pytest.MonkeyPatch,
    isolated_dirs: IsolatedDirs,
    mock_anthropic_proxy: MockProxyFixture,
    system_bazel: str,
) -> None:
    """Set up environment for running the session start hook (podman disabled)."""
    _setup_hook_env(monkeypatch, isolated_dirs, mock_anthropic_proxy.proxy, system_bazel, skip_podman=True)


def make_hook_input(project_dir: Path, source: HookSource = HookSource.STARTUP) -> str:
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
    project_dir: Path, source: HookSource = HookSource.STARTUP
) -> subprocess.CompletedProcess[str]:
    """Run the session start hook (inherits environment from os.environ via monkeypatch).

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

    result = subprocess.run([cmd], check=False, input=hook_input, capture_output=True, text=True, timeout=300)

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
    def test_session_start_succeeds(self, isolated_dirs: IsolatedDirs, hook_env: None) -> None:
        """Run full session start hook and verify it succeeds."""
        result = run_session_start_hook(isolated_dirs.project)

        assert result.returncode == 0, f"Hook failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

        # Verify key artifacts created
        # platformdirs respects XDG_CACHE_HOME
        bazel_proxy_dir = isolated_dirs.cache / "claude-hooks" / "bazel-proxy"
        assert (bazel_proxy_dir / "bazelrc").exists(), "bazelrc not created"
        assert (bazel_proxy_dir / "anthropic_ca.pem").exists(), "CA not extracted"

        # Verify supervisor started
        # platformdirs respects XDG_CONFIG_HOME
        supervisor_dir = isolated_dirs.config / "claude-hooks" / "supervisor"
        assert (supervisor_dir / "supervisord.pid").exists(), "supervisor not started"

    @pytest.mark.skipif(not shutil.which("keytool"), reason="keytool required")
    @pytest.mark.skipif(not shutil.which("bazel") and not shutil.which("bazelisk"), reason="bazel/bazelisk required")
    def test_bazel_build_after_hook(
        self, isolated_dirs: IsolatedDirs, hook_env: None, mock_anthropic_proxy: MockProxyFixture
    ) -> None:
        """Run hook, then verify bazel can build through the proxy."""
        result = run_session_start_hook(isolated_dirs.project)
        assert result.returncode == 0, f"Hook failed: {result.stderr}"

        # Copy testdata workspace to test location
        # This is a minimal bzlmod workspace with no external dependencies, so the mock
        # MockAnthropicProxy (which can't do real DNS/forwarding) isn't a blocker.
        test_file_dir = Path(__file__).parent
        testdata_workspace = test_file_dir / "testdata" / "test_workspace"
        workspace = isolated_dirs.project / "test_workspace"
        shutil.copytree(testdata_workspace, workspace)

        # Use isolated output_base to prevent conflicts with the outer Bazel running this test
        output_base = isolated_dirs.cache / "bazel_output_base"
        output_base.mkdir(parents=True, exist_ok=True)

        # Run bazel build in a shell that sources the env file (like Claude Code would)
        # The env file adds the wrapper dir to PATH, sets proxy vars to local auth-proxy,
        # and exports truststore configuration. The wrapper injects --bazelrc and falls
        # back to system bazel if bazelisk isn't installed.
        # --output_base isolates this Bazel from the test-running Bazel.
        result = shell_helpers.run_with_env_file(
            command=f"bazel --output_base={output_base} build //:hello",
            env_file=isolated_dirs.env_file,
            cwd=workspace,
            check=False,
            timeout=300,
        )

        # Collect logs to TEST_UNDECLARED_OUTPUTS_DIR for artifact collection
        supervisor_dir = isolated_dirs.config / "claude-hooks" / "supervisor"
        cache_dir = isolated_dirs.cache / "claude-hooks"
        outputs_dir = Path(os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", "/tmp/test-outputs"))
        outputs_dir.mkdir(parents=True, exist_ok=True)
        # Note: mock_proxy_log is already in outputs_dir (created by fixture), no need to copy
        all_logs = [
            (supervisor_dir / "supervisord.log", "supervisord.log"),
            (supervisor_dir / "bazel-proxy.log", "bazel-proxy.log"),
            (supervisor_dir / "bazel-proxy.err.log", "bazel-proxy.err.log"),
            (cache_dir / "session-start.log", "session-start.log"),
        ]
        if mock_anthropic_proxy.log_file.exists():
            all_logs.append((mock_anthropic_proxy.log_file, "mock-anthropic-proxy.log"))
        for log_path, log_name in all_logs:
            if log_path.exists():
                dest = outputs_dir / log_name
                if log_path.resolve() != dest.resolve():
                    shutil.copy(log_path, dest)

        assert result.returncode == 0, f"Bazel build failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

    @pytest.mark.skipif(not shutil.which("keytool"), reason="keytool required")
    def test_stale_socket_recovery(self, isolated_dirs: IsolatedDirs, hook_env: None) -> None:
        """Verify hook recovers from stale supervisor socket."""
        # Create stale socket/pidfile
        # platformdirs respects XDG_CONFIG_HOME
        supervisor_dir = isolated_dirs.config / "claude-hooks" / "supervisor"
        supervisor_dir.mkdir(parents=True, exist_ok=True)
        (supervisor_dir / "supervisor.sock").touch()
        (supervisor_dir / "supervisord.pid").write_text("99999")  # Non-existent PID

        result = run_session_start_hook(isolated_dirs.project)

        assert result.returncode == 0, f"Hook failed with stale socket:\nstderr: {result.stderr}"

    @pytest.mark.skipif(not shutil.which("keytool"), reason="keytool required")
    def test_resume_event(self, isolated_dirs: IsolatedDirs, hook_env: None) -> None:
        """Test that resume events also work correctly."""
        result = run_session_start_hook(isolated_dirs.project, source=HookSource.RESUME)

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
    def podman_hook_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_dirs: IsolatedDirs,
        mock_anthropic_proxy: MockProxyFixture,
        system_bazel: str,
    ) -> None:
        """Set up environment for running session start hook WITH podman enabled."""
        _setup_hook_env(monkeypatch, isolated_dirs, mock_anthropic_proxy.proxy, system_bazel, skip_podman=False)

    @pytest.mark.skipif(not shutil.which("keytool"), reason="keytool required")
    @pytest.mark.skipif(not _can_use_podman(), reason="podman not installed")
    def test_podman_service_starts(self, isolated_dirs: IsolatedDirs, podman_hook_env: None) -> None:
        """Verify podman service starts after session start hook."""
        result = run_session_start_hook(isolated_dirs.project)

        assert result.returncode == 0, "Hook failed with non-zero exit code"

        socket_path = _extract_docker_host_socket(isolated_dirs.env_file)
        assert socket_path.exists(), f"Podman socket not created at {socket_path}"

    @pytest.mark.skipif(not shutil.which("keytool"), reason="keytool required")
    @pytest.mark.skipif(not _can_use_podman(), reason="podman not installed")
    def test_podman_can_run_container(
        self, isolated_dirs: IsolatedDirs, podman_hook_env: None, mock_anthropic_proxy: MockProxyFixture
    ) -> None:
        """Verify podman can run a container after session start hook.

        Runs podman through the MockAnthropicProxy to verify the full proxy chain works,
        including CA certificate configuration for container registry pulls.
        """
        result = run_session_start_hook(isolated_dirs.project)

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
        )

        # Include proxy stats in failure message for debugging
        proxy = mock_anthropic_proxy.proxy
        proxy_stats = (
            f"\nProxy stats: {proxy.stats.total_connections} total, "
            f"{proxy.stats.successful_connections} success, "
            f"{proxy.stats.failed_connections} failed"
        )
        if proxy.stats.errors:
            proxy_stats += f"\nProxy errors: {proxy.stats.errors[-5:]}"

        assert podman_result.returncode == 0, (
            f"Podman run failed:\nstdout: {podman_result.stdout}\nstderr: {podman_result.stderr}{proxy_stats}"
        )
        assert "Hello from Docker" in podman_result.stdout, (
            f"Expected 'Hello from Docker' in output:\n{podman_result.stdout}{proxy_stats}"
        )


if __name__ == "__main__":
    pytest_bazel.main()
