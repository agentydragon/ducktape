"""Shared test helpers for session_start hook e2e tests.

Provides isolated directory setup, environment configuration, and hook execution
utilities used by both the main session_start tests and the podman integration tests.
"""

import asyncio
import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest

from devinfra.claude import settings
from devinfra.claude.auth_proxy.setup import SSL_CA_ENV_VARS, SYSTEM_CA_BUNDLES
from devinfra.claude.auth_proxy.vars import PROXY_ENV_VARS
from devinfra.claude.claude_api.hooks.common import PermissionMode
from devinfra.claude.claude_api.hooks.session_start import HookSource, SessionStartHookInput
from devinfra.claude.testing import shell_helpers
from devinfra.claude.testing.mock_egress_proxy import MockEgressProxy
from devinfra.claude.tmpfs_setup import unmount_tmpfs_under
from util.bazel.runfiles import get_required_path
from util.net import pick_free_port
from util.testing.undeclared_outputs import undeclared_outputs_dir

TEST_SESSION_ID = "test-session-123"


@dataclass
class IsolatedDirs:
    """Isolated directories for e2e tests."""

    home: Path
    project: Path
    cache: Path
    config: Path
    runtime: Path
    session_dir: Path
    env_file: Path


@pytest.fixture
def isolated_dirs(tmp_path: Path) -> IsolatedDirs:
    """Create isolated directories for the test."""
    home = tmp_path / "home"
    # Mirror real Claude Code layout: ~/.claude/session-env/<session_id>/
    session_dir = home / ".claude" / "session-env" / TEST_SESSION_ID
    dirs = IsolatedDirs(
        home=home,
        project=tmp_path / "project",
        cache=tmp_path / "cache",
        config=tmp_path / "config",
        runtime=tmp_path / "runtime",
        session_dir=session_dir,
        env_file=session_dir / "sessionstart-hook-0.sh",
    )
    dirs.home.mkdir()
    dirs.session_dir.mkdir(parents=True)
    dirs.project.mkdir()
    dirs.cache.mkdir()
    dirs.config.mkdir()
    dirs.runtime.mkdir()
    (dirs.project / ".git").mkdir()
    return dirs


def setup_hook_env(
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


def make_hook_input(project_dir: Path, source: HookSource = HookSource.STARTUP) -> str:
    """Create JSON input that Claude Code would send to the hook."""
    return SessionStartHookInput(
        session_id=TEST_SESSION_ID,
        cwd=project_dir,
        transcript_path=Path("/tmp/transcript.json"),
        permission_mode=PermissionMode.DEFAULT,
        source=source,
        model="claude-sonnet-4-6",
    ).model_dump_json()


def cleanup_supervisor(config_dir: Path) -> None:
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


def write_output_log(name: str, content: str) -> Path:
    """Write content to a log file in the outputs directory."""
    log_path = undeclared_outputs_dir() / name
    log_path.write_text(content)
    return log_path


async def run_session_start_hook(
    project_dir: Path, source: HookSource = HookSource.STARTUP
) -> subprocess.CompletedProcess[str]:
    """Run the session start hook as an async subprocess.

    By default, runs via `python -m devinfra.claude.hook_daemon.session_start` for Bazel tests.
    Set DUCKTAPE_CLAUDE_HOOKS_USE_WHEEL=1 to run via the installed `claude-hook` console
    script instead - this tests the actual wheel packaging.

    Hook output is written to log files in TEST_UNDECLARED_OUTPUTS_DIR for debugging.
    """
    hook_input = make_hook_input(project_dir, source)

    use_wheel = os.environ.get(settings.ENV_USE_WHEEL) == "1"

    cmd: str | Path = "claude-hook" if use_wheel else get_required_path(shell_helpers.HOOK_DISPATCH)

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
    stdout_log = write_output_log("hook-stdout.log", result.stdout)
    stderr_log = write_output_log("hook-stderr.log", result.stderr)
    print(f"Hook output written to: {stdout_log}, {stderr_log}")

    return result


@pytest.fixture(autouse=True)
def cleanup_after_test(isolated_dirs: IsolatedDirs) -> Generator[None]:
    """Cleanup supervisor and tmpfs mounts after each test."""
    yield
    unmount_tmpfs_under(isolated_dirs.session_dir)
    cleanup_supervisor(isolated_dirs.session_dir)
