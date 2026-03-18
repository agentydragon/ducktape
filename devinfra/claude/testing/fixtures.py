"""Shared test fixtures for claude tests.

Import and use in test files - Bazel doesn't do conftest.py auto-discovery.
"""

import contextlib
import logging
import os
import shutil
import signal
import time
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from pathlib import Path

import pytest

from devinfra.claude import settings
from devinfra.claude.session_paths import SessionPaths
from devinfra.claude.settings import HookSettings
from devinfra.claude.supervisor.client import try_connect
from devinfra.claude.testing.mock_egress_proxy import EgressProxyConfig, MockEgressProxy
from util.net import pick_free_port
from util.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)


@dataclass
class MockEgressProxyFixture:
    """Container for mock egress proxy and its associated log file."""

    proxy: MockEgressProxy
    log_file: Path


@dataclass
class IsolatedSupervisorDirs:
    """Isolated directories for supervisor/proxy testing."""

    session_dir: Path
    supervisor_dir: Path
    auth_proxy_dir: Path


TEST_SESSION_ID = "test-session"


@pytest.fixture
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[IsolatedSupervisorDirs]:
    """Create isolated session/supervisor/auth-proxy dirs with free ports.

    Sets HOME so SessionPaths derives paths under tmp_path.
    Sets environment variables so HookSettings() picks them up.
    Cleans up any supervisor processes on teardown.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # SessionPaths.from_env(TEST_SESSION_ID, env) will derive: home/.claude/session-env/test-session
    session_dir = home / ".claude" / "session-env" / TEST_SESSION_ID
    session_dir.mkdir(parents=True)
    supervisor_dir = session_dir / "supervisor"
    supervisor_dir.mkdir()
    auth_proxy_dir = session_dir / "auth-proxy"
    auth_proxy_dir.mkdir()

    monkeypatch.setenv(settings.ENV_SESSION_DIR, str(session_dir))
    monkeypatch.setenv(settings.ENV_SUPERVISOR_PORT, str(pick_free_port()))
    monkeypatch.setenv(settings.ENV_AUTH_PROXY_PORT, str(pick_free_port()))

    with supervisor_cleanup(supervisor_dir / "supervisord.pid"):
        yield IsolatedSupervisorDirs(
            session_dir=session_dir, supervisor_dir=supervisor_dir, auth_proxy_dir=auth_proxy_dir
        )

    # Collect supervisor logs into test outputs for CI debugging
    collect_supervisor_logs(supervisor_dir)


@pytest.fixture
def session_paths(isolated_dirs: IsolatedSupervisorDirs) -> SessionPaths:
    """SessionPaths wired to isolated dirs (reads monkeypatched HOME from os.environ)."""
    return SessionPaths.from_env(TEST_SESSION_ID, dict(os.environ))


@pytest.fixture
def hook_settings() -> HookSettings:
    """HookSettings wired to isolated dirs."""
    return HookSettings()


@pytest.fixture
async def mock_egress_proxy() -> AsyncGenerator[MockEgressProxyFixture]:
    """Mock of Anthropic's TLS-inspecting egress proxy that chains through upstream if available.

    Works in gVisor environments by detecting HTTPS_PROXY and chaining through it.
    Configures file logging for debugging proxy behavior in CI.

    Yields a MockEgressProxyFixture with both the proxy and its log file path.
    """
    log_file = undeclared_outputs_dir() / "mock-egress-proxy.log"

    proxy_logger = logging.getLogger("devinfra.claude.testing.mock_egress_proxy")
    proxy_logger.setLevel(logging.DEBUG)

    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    handler.setLevel(logging.DEBUG)
    proxy_logger.addHandler(handler)

    try:
        async with MockEgressProxy(
            listen_port=0, username="proxy_user", password="test_jwt_token", upstream_proxy=EgressProxyConfig.from_env()
        ) as proxy:
            yield MockEgressProxyFixture(proxy=proxy, log_file=log_file)
    finally:
        handler.close()
        proxy_logger.removeHandler(handler)


def collect_supervisor_logs(supervisor_dir: Path) -> None:
    """Copy supervisor files to undeclared test outputs for CI artifact collection.

    Recursively collects all regular files (logs, config, pidfile, conf.d/ contents)
    from the supervisor directory tree.
    """
    if not supervisor_dir.exists():
        return

    dest = undeclared_outputs_dir() / "supervisor-logs"
    dest.mkdir(parents=True, exist_ok=True)

    for f in supervisor_dir.rglob("*"):
        if not f.is_file():
            continue
        relative = f.relative_to(supervisor_dir)
        target = dest / relative
        if f.resolve() == target.resolve():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(f, target)
        except OSError as e:
            logger.warning("Failed to collect supervisor file %s: %s", f, e)


# === Supervisor lifecycle helpers ===


async def supervisor_is_running(paths: SessionPaths, settings: HookSettings) -> bool:
    """Check if supervisord is running (test helper)."""
    return await try_connect(paths, settings) is not None


def stop_supervisor_by_pidfile(pidfile: Path) -> None:
    """Stop supervisor process by reading and killing from pidfile."""
    if not pidfile.exists():
        return
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        time.sleep(0.2)
    except (ValueError, ProcessLookupError, OSError):
        pass
    with contextlib.suppress(OSError):
        pidfile.unlink()


@contextlib.contextmanager
def supervisor_cleanup(pidfile: Path) -> Generator[None]:
    """Context manager for supervisor cleanup before and after test."""
    stop_supervisor_by_pidfile(pidfile)
    try:
        yield
    finally:
        stop_supervisor_by_pidfile(pidfile)
