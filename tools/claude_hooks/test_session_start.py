"""Tests for session_start hook.

Includes:
- HookInput parsing tests (unit)
- Full hook subprocess tests (e2e) with MockEgressProxy simulating Anthropic's egress proxy
"""

from __future__ import annotations

import asyncio
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

from bazel_util.runfiles import get_required_path
from net_util.net import pick_free_port
from test_util.undeclared_outputs import undeclared_outputs_dir
from tools.claude_hooks import settings
from tools.claude_hooks.proxy_setup import SSL_CA_ENV_VARS, SYSTEM_CA_BUNDLES
from tools.claude_hooks.proxy_vars import PROXY_ENV_VARS
from tools.claude_hooks.session_start import HookInput, HookSource
from tools.claude_hooks.testing import shell_helpers
from tools.claude_hooks.testing.fixtures import MockEgressProxyFixture, collect_supervisor_logs
from tools.claude_hooks.testing.mock_egress_proxy import MockEgressProxy

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
    runtime: Path
    env_file: Path


@pytest.fixture
def isolated_dirs(tmp_path: Path) -> IsolatedDirs:
    """Create isolated directories for the test."""
    dirs = IsolatedDirs(
        home=tmp_path / "home",
        project=tmp_path / "project",
        cache=tmp_path / "cache",
        config=tmp_path / "config",
        runtime=tmp_path / "runtime",
        env_file=tmp_path / "env.sh",
    )
    dirs.home.mkdir()
    dirs.project.mkdir()
    dirs.cache.mkdir()
    dirs.config.mkdir()
    dirs.runtime.mkdir()
    (dirs.project / ".git").mkdir()
    return dirs


def _setup_hook_env(
    monkeypatch: pytest.MonkeyPatch,
    isolated_dirs: IsolatedDirs,
    mock_proxy: MockEgressProxy,
    *,
    container_runtime: str = "none",
) -> None:
    """Set up environment variables for running session start hook via monkeypatch.

    Args:
        monkeypatch: pytest monkeypatch fixture
        isolated_dirs: Test isolation directories
        mock_proxy: TLS proxy simulating Anthropic's proxy
        container_runtime: Container runtime to use ("none", "podman", "docker")
    """
    # Create combined CA bundle with system CAs + mock proxy CA
    # This allows bazelisk and other TLS clients to trust the mock proxy
    system_ca_path = next((p for p in SYSTEM_CA_BUNDLES if p.exists()), None)
    combined_ca_path = isolated_dirs.cache / "combined_ca.pem"
    system_cas = system_ca_path.read_bytes() if system_ca_path else b""
    combined_ca_path.write_bytes(system_cas + b"\n" + mock_proxy.ca_cert_pem)

    # Pick isolated ports for supervisor and auth proxy
    supervisor_port = pick_free_port()
    auth_proxy_port = pick_free_port()

    # Required for web mode
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(isolated_dirs.project))
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(isolated_dirs.env_file))

    # Isolated directories
    monkeypatch.setenv("HOME", str(isolated_dirs.home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(isolated_dirs.cache))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated_dirs.config))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(isolated_dirs.runtime))

    # Isolated ports (avoid conflicts between tests)
    monkeypatch.setenv(settings.ENV_SUPERVISOR_PORT, str(supervisor_port))
    monkeypatch.setenv(settings.ENV_AUTH_PROXY_PORT, str(auth_proxy_port))

    # Disable nix (speeds up tests)
    monkeypatch.setenv(settings.ENV_INSTALL_NIX, "0")

    # Proxy configuration (simulating Claude Code web)
    for var in PROXY_ENV_VARS:
        monkeypatch.setenv(var, mock_proxy.url)

    # Configure SSL trust for the mock proxy's CA
    # This is needed for bazelisk and other tools to trust TLS connections through the mock proxy
    for var in SSL_CA_ENV_VARS:
        monkeypatch.setenv(var, str(combined_ca_path))

    # Point _extract_proxy_ca to the mock CA on the filesystem
    mock_ca_path = isolated_dirs.cache / "mock-anthropic-ca.crt"
    mock_ca_path.write_bytes(mock_proxy.ca_cert_pem)
    monkeypatch.setenv("ANTHROPIC_CA_PATH", str(mock_ca_path))

    monkeypatch.setenv(settings.ENV_CONTAINER_RUNTIME, container_runtime)


@pytest.fixture
def hook_env(
    monkeypatch: pytest.MonkeyPatch, isolated_dirs: IsolatedDirs, mock_egress_proxy: MockEgressProxyFixture
) -> None:
    """Set up environment for running the session start hook (container runtime disabled)."""
    _setup_hook_env(monkeypatch, isolated_dirs, mock_egress_proxy.proxy, container_runtime="none")


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


def _write_output_log(name: str, content: str) -> Path:
    """Write content to a log file in the outputs directory."""
    log_path = undeclared_outputs_dir() / name
    log_path.write_text(content)
    return log_path


def _save_bazel_logs(result: subprocess.CompletedProcess[str], label: str) -> None:
    """Save bazel invocation stdout/stderr to undeclared test outputs."""
    out_dir = undeclared_outputs_dir() / f"bazel-{label}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if result.stdout:
        (out_dir / "stdout.log").write_text(result.stdout)
    if result.stderr:
        (out_dir / "stderr.log").write_text(result.stderr)


async def run_session_start_hook(
    project_dir: Path, source: HookSource = HookSource.STARTUP
) -> subprocess.CompletedProcess[str]:
    """Run the session start hook as an async subprocess.

    By default, runs via `python -m tools.claude_hooks.session_start` for Bazel tests.
    Set DUCKTAPE_CLAUDE_HOOKS_USE_WHEEL=1 to run via the installed `claude-session-start` console
    script instead - this tests the actual wheel packaging.

    Hook output is written to log files in TEST_UNDECLARED_OUTPUTS_DIR for debugging.
    """
    hook_input = make_hook_input(project_dir, source)

    use_wheel = os.environ.get(settings.ENV_USE_WHEEL) == "1"

    cmd: str | Path = "claude-session-start" if use_wheel else get_required_path(shell_helpers.SESSION_START)

    env = dict(os.environ)
    if use_wheel:
        # Bazel's test runner sets PYTHONPATH to all runfiles site-packages.
        # The subprocess inherits this, so it can import packages (like httpx)
        # from Bazel's deps even though they're missing from the wheel's
        # requires list. Clear it so only the wheel venv's packages are visible.
        env.pop("PYTHONPATH", None)

    proc = await asyncio.create_subprocess_exec(
        cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(input=hook_input.encode()), timeout=300)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise subprocess.TimeoutExpired(cmd=[cmd], timeout=300)
    result = subprocess.CompletedProcess(
        args=[cmd],
        returncode=proc.returncode or 0,
        stdout=stdout_bytes.decode() if stdout_bytes else "",
        stderr=stderr_bytes.decode() if stderr_bytes else "",
    )

    # Write hook output to log files for debugging (collected as CI artifacts)
    stdout_log = _write_output_log("hook-stdout.log", result.stdout)
    stderr_log = _write_output_log("hook-stderr.log", result.stderr)
    print(f"Hook output written to: {stdout_log}, {stderr_log}")

    return result


@pytest.fixture(autouse=True)
def cleanup_after_test(isolated_dirs: IsolatedDirs) -> Generator[None]:
    """Cleanup supervisor after each test."""
    yield
    _cleanup_supervisor(isolated_dirs.env_file.parent)


class TestFullSessionStartHook:
    """E2E tests running the complete session start hook."""

    async def test_session_start_succeeds(self, isolated_dirs: IsolatedDirs, hook_env: None) -> None:
        """Run full session start hook and verify it succeeds."""
        result = await run_session_start_hook(isolated_dirs.project)

        assert result.returncode == 0, f"Hook failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

        # Verify key artifacts created
        # Session bazelrc is now in session directory (parent of env_file)
        session_dir = isolated_dirs.env_file.parent
        assert (session_dir / "bazelrc").exists(), "bazelrc not created in session directory"

        # Auth proxy artifacts in session directory
        auth_proxy_dir = isolated_dirs.env_file.parent / "auth-proxy"
        assert (auth_proxy_dir / "anthropic_ca.pem").exists(), "CA not extracted"

        # Verify supervisor started
        supervisor_dir = isolated_dirs.env_file.parent / "supervisor"
        assert (supervisor_dir / "supervisord.pid").exists(), "supervisor not started"

    async def test_bazel_build_after_hook(
        self, isolated_dirs: IsolatedDirs, hook_env: None, mock_egress_proxy: MockEgressProxyFixture
    ) -> None:
        """Run hook, then verify bazel can build through the proxy."""
        result = await run_session_start_hook(isolated_dirs.project)
        assert result.returncode == 0, f"Hook failed: {result.stderr}"

        # Copy testdata workspace to test location
        # This is a minimal bzlmod workspace with no external dependencies, so the mock
        # MockEgressProxy (which can't do real DNS/forwarding) isn't a blocker.
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
        supervisor_dir = isolated_dirs.env_file.parent / "supervisor"
        bazel_result: subprocess.CompletedProcess[str] | None = None
        try:
            async with asyncio.timeout(60):
                bazel_result = await shell_helpers.run_with_env_file(
                    command=f"bazel --output_base={output_base} build //:hello",
                    env_file=isolated_dirs.env_file,
                    cwd=workspace,
                )
                assert bazel_result.returncode == 0, (
                    f"Bazel build failed (rc={bazel_result.returncode}):\n"
                    f"stdout: {bazel_result.stdout}\n"
                    f"stderr: {bazel_result.stderr}"
                )
        finally:
            if bazel_result is not None:
                _save_bazel_logs(bazel_result, "build-hello")
            # Always collect logs - critical for debugging CI failures
            collect_supervisor_logs(supervisor_dir)

    async def test_stale_socket_recovery(self, isolated_dirs: IsolatedDirs, hook_env: None) -> None:
        """Verify hook recovers from stale supervisor socket."""
        # Create stale socket/pidfile
        supervisor_dir = isolated_dirs.env_file.parent / "supervisor"
        supervisor_dir.mkdir(parents=True, exist_ok=True)
        (supervisor_dir / "supervisor.sock").touch()
        (supervisor_dir / "supervisord.pid").write_text("99999")  # Non-existent PID

        result = await run_session_start_hook(isolated_dirs.project)

        assert result.returncode == 0, f"Hook failed with stale socket:\nstderr: {result.stderr}"

    async def test_resume_event(self, isolated_dirs: IsolatedDirs, hook_env: None) -> None:
        """Test that resume events also work correctly."""
        result = await run_session_start_hook(isolated_dirs.project, source=HookSource.RESUME)

        assert result.returncode == 0, f"Hook failed on resume:\nstderr: {result.stderr}"

    async def test_secrets_decrypted_into_env_file(
        self, monkeypatch: pytest.MonkeyPatch, isolated_dirs: IsolatedDirs, hook_env: None
    ) -> None:
        """Run hook with age key set, verify decrypted secret is visible in env file."""
        # Test keypair (committed in testdata — NOT a real secret)
        test_age_key = "AGE-SECRET-KEY-1DVR9RHP2MVZYD6HE46W4JNWMA673U8FYS00TCLX9VNXCFMQJX5ZQTUEP9E"

        # Symlink .claude_hooks/ from testdata into the test project dir (read-only, no copy needed).
        # Can't point CLAUDE_PROJECT_DIR at runfiles directly because the hook writes to .git/hooks/.
        test_secrets_src = Path(__file__).parent / "testdata" / "test_secrets" / ".claude_hooks"
        (isolated_dirs.project / ".claude_hooks").symlink_to(test_secrets_src)

        monkeypatch.setenv(settings.ENV_PREFIX + "SECRETS_AGE_KEY", test_age_key)

        result = await run_session_start_hook(isolated_dirs.project)
        assert result.returncode == 0, f"Hook failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

        # Verify the secret is visible when sourcing the env file
        shell_result = await shell_helpers.run_with_env_file(
            command="echo $TEST_SECRET_TOKEN", env_file=isolated_dirs.env_file, check=True
        )
        assert shell_result.stdout.strip() == "test-value-12345"


def _can_use_podman() -> bool:
    """Check if podman is available for use.

    Returns True if podman is already installed.
    The test target uses local=True so podman can create user namespaces.
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
        self, monkeypatch: pytest.MonkeyPatch, isolated_dirs: IsolatedDirs, mock_egress_proxy: MockEgressProxyFixture
    ) -> None:
        """Set up environment for running session start hook WITH podman enabled."""
        _setup_hook_env(monkeypatch, isolated_dirs, mock_egress_proxy.proxy, container_runtime="podman")

    @pytest.mark.skipif(not _can_use_podman(), reason="podman not installed")
    async def test_podman_can_run_container(
        self, isolated_dirs: IsolatedDirs, podman_hook_env: None, mock_egress_proxy: MockEgressProxyFixture
    ) -> None:
        """Verify podman service starts and can run a container after session start hook.

        Runs podman through the MockEgressProxy to verify the full proxy chain works,
        including CA certificate configuration for container registry pulls.
        """
        result = await run_session_start_hook(isolated_dirs.project)

        assert result.returncode == 0, "Hook failed with non-zero exit code"

        socket_path = _extract_docker_host_socket(isolated_dirs.env_file)
        assert socket_path.exists(), f"Podman socket not created at {socket_path}"

        # Collect supervisor logs (including podman daemon) for CI debugging
        supervisor_dir = isolated_dirs.env_file.parent / "supervisor"
        collect_supervisor_logs(supervisor_dir)

        # Verify we can run podman hello-world through the proxy
        # The gVisor annotation is auto-applied via containers.conf
        # Run through env file to pick up SSL_CERT_FILE for TLS proxy CA
        async with asyncio.timeout(120):
            podman_result = await shell_helpers.run_with_env_file(
                command="podman run --rm docker.io/library/hello-world",
                env_file=isolated_dirs.env_file,
                cwd=isolated_dirs.project,
                check=False,
            )

        # Include proxy stats in failure message for debugging
        proxy = mock_egress_proxy.proxy
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
