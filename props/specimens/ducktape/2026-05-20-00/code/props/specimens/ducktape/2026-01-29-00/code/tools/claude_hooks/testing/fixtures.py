"""Shared test fixtures for claude_hooks tests.

Import and use in test files - Bazel doesn't do conftest.py auto-discovery.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest

from net_util.net import pick_free_port
from tools.claude_hooks import settings
from tools.claude_hooks.settings import HookSettings
from tools.claude_hooks.supervisor.client import try_connect
from tools.claude_hooks.testing.mock_egress_proxy import EgressProxyConfig, MockEgressProxy

logger = logging.getLogger(__name__)


@dataclass
class MockEgressProxyFixture:
    """Container for mock egress proxy and its associated log file."""

    proxy: MockEgressProxy
    log_file: Path


@dataclass
class IsolatedSupervisorDirs:
    """Isolated directories for supervisor/proxy testing."""

    supervisor_dir: Path
    auth_proxy_dir: Path


@pytest.fixture
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[IsolatedSupervisorDirs]:
    """Create isolated supervisor + auth proxy dirs with free ports.

    Sets environment variables so HookSettings() picks them up.
    Cleans up any supervisor processes on teardown.
    """
    supervisor_dir = tmp_path / "supervisor"
    supervisor_dir.mkdir()
    auth_proxy_dir = tmp_path / "auth-proxy"
    auth_proxy_dir.mkdir()

    monkeypatch.setenv(settings.ENV_SUPERVISOR_DIR, str(supervisor_dir))
    monkeypatch.setenv(settings.ENV_SUPERVISOR_PORT, str(pick_free_port()))
    monkeypatch.setenv(settings.ENV_AUTH_PROXY_DIR, str(auth_proxy_dir))
    monkeypatch.setenv(settings.ENV_AUTH_PROXY_PORT, str(pick_free_port()))

    with supervisor_cleanup(supervisor_dir / "supervisord.pid"):
        yield IsolatedSupervisorDirs(supervisor_dir=supervisor_dir, auth_proxy_dir=auth_proxy_dir)

    # Collect supervisor logs into test outputs for CI debugging
    collect_supervisor_logs(supervisor_dir)


@pytest.fixture
def hook_settings(isolated_dirs: IsolatedSupervisorDirs) -> HookSettings:
    """HookSettings wired to isolated dirs."""
    return HookSettings()


@pytest.fixture(scope="module")
def mock_egress_proxy() -> Generator[MockEgressProxyFixture]:
    """Mock of Anthropic's TLS-inspecting egress proxy that chains through upstream if available.

    Works in gVisor environments by detecting HTTPS_PROXY and chaining through it.
    Configures file logging for debugging proxy behavior in CI.

    Yields a MockEgressProxyFixture with both the proxy and its log file path.
    """
    outputs_dir = Path(os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", "/tmp/test-outputs"))
    outputs_dir.mkdir(parents=True, exist_ok=True)
    log_file = outputs_dir / "mock-egress-proxy.log"

    proxy_logger = logging.getLogger("tools.claude_hooks.testing.mock_egress_proxy")
    proxy_logger.setLevel(logging.DEBUG)

    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    handler.setLevel(logging.DEBUG)
    proxy_logger.addHandler(handler)

    try:
        with MockEgressProxy(
            listen_port=0,
            require_auth=True,
            username="proxy_user",
            password="test_jwt_token",
            upstream_proxy=EgressProxyConfig.from_env(),
        ) as proxy:
            yield MockEgressProxyFixture(proxy=proxy, log_file=log_file)
    finally:
        handler.close()
        proxy_logger.removeHandler(handler)


def collect_supervisor_logs(supervisor_dir: Path) -> None:
    """Copy supervisor files to TEST_UNDECLARED_OUTPUTS_DIR for CI artifact collection.

    Recursively collects all regular files (logs, config, pidfile, conf.d/ contents)
    from the supervisor directory tree.
    No-op if TEST_UNDECLARED_OUTPUTS_DIR is not set.
    """
    outputs_dir = os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR")
    if not outputs_dir:
        return
    if not supervisor_dir.exists():
        return

    dest = Path(outputs_dir) / "supervisor-logs"
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


async def supervisor_is_running(settings_obj: HookSettings) -> bool:
    """Check if supervisord is running (test helper)."""
    return await try_connect(settings_obj) is not None


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
