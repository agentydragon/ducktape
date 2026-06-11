import importlib
import importlib.util
import json
import os
import shlex
import shutil
import socket
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pygit2
import pytest
import yaml
from typer.testing import CliRunner

from wt.server import github_client
from wt.server.git_manager import GitManager
from wt.server.gitstatusd_listener import find_gitstatusd_in_runfiles
from wt.server.worktree_service import WorktreeService
from wt.shared.config_file import ConfigFile
from wt.shared.configuration import Configuration
from wt.shared.fixtures import write_pr_fixtures_file
from wt.shared.protocol import (
    CommitInfo,
    DaemonHealth,
    DaemonHealthStatus,
    PRInfoDisabled,
    StatusItem,
    StatusResponse,
    StatusResult,
    StatusResultOk,
)
from wt.testing.config_factory import ConfigFactory
from wt.testing.mock_factory import MockFactory, ServiceBuilder
from wt.testing.repo_factory import GitRepoFactory
from wt.testing.utils import run_cli_command, wait_until


def get_wt_package_dir() -> Path:
    """Return the installed wt package directory."""
    wt_file = importlib.import_module("wt").__file__
    assert wt_file is not None
    return Path(wt_file).parent


@pytest.fixture(scope="session", autouse=True)
def _project_root_on_pythonpath():
    """Set a global test-mode env var for the WT suite without monkeypatch.

    Session-scoped fixtures cannot depend on the function-scoped monkeypatch fixture.
    Use direct os.environ mutation with a restore on teardown instead.
    """
    prev = os.environ.get("WT_TEST_MODE")
    os.environ["WT_TEST_MODE"] = "1"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("WT_TEST_MODE", None)
        else:
            os.environ["WT_TEST_MODE"] = prev


@pytest.fixture
def write_pr_fixtures():
    """Write typed PR fixtures to $WT_DIR/pr_fixtures.json.

    Example:
        write_pr_fixtures(config, {"feature-x": PRFixtureEntry(number=123, ...)})
    """

    return write_pr_fixtures_file


@pytest.fixture(autouse=True)
def _disable_gh_cli_token(monkeypatch):
    """Disable gh CLI token retrieval in all tests by default.

    Tests that truly need real GitHub should explicitly bypass or override this.
    """
    monkeypatch.setattr(github_client, "get_github_token", lambda *a, **kw: None)


# =============================================================================
# Factory Fixtures - Modern pytest pattern for test setup
#
# These factories replace the old pattern of many specific fixtures with
# flexible, parameterizable factories. Use these patterns:
#
# OLD WAY (being phased out):
#   def test_something(git_repo, mock_github_interface):
#       # Uses hard-coded test repo and mock
#
# NEW WAY (preferred):
#   def test_something(repo_factory, mock_factory):
#       repo = repo_factory.create_repo(**RepoPresets.with_branches())
#       github = mock_factory.github_client(pr_list_returns=[...])
#
# Benefits:
# - Explicit test data (no hidden setup)
# - Parameterizable (different configs per test)
# - Less fixture coupling
# - Easier to understand and maintain
# =============================================================================


@pytest.fixture
def mock_factory():
    """Factory for creating configured mocks with standard behaviors."""
    return MockFactory


@pytest.fixture
def service_builder():
    """Lowercase alias for ServiceBuilder to match legacy tests."""
    return ServiceBuilder


@pytest.fixture
def repo_factory(temp_dir):
    """Factory for creating git repositories with different configurations."""
    return GitRepoFactory(temp_dir)


@pytest.fixture
def pygit2_repo(real_temp_repo) -> pygit2.Repository:
    """Provide a pygit2.Repository instance for the test's real_temp_repo.

    Use this fixture when you need to perform multiple pygit2 operations
    on the same repository, avoiding repeated Repository instantiation.

    Example:
        def test_something(pygit2_repo, real_temp_repo):
            # Use pygit2_repo for git operations
            branch = pygit2_repo.head.shorthand
            # Use real_temp_repo for path operations
            (real_temp_repo / "file.txt").write_text("content")
    """
    return pygit2.Repository(real_temp_repo)


@pytest.fixture
def config_factory(temp_dir):
    """Factory for creating test configurations with presets and overrides."""

    def _factory_for_repo(repo_path: Path):
        return ConfigFactory(repo_path, temp_dir)

    return _factory_for_repo


# Service builder and env var fixtures removed - configure directly in tests with factories


@pytest.fixture
def cli_test_env(repo_factory, config_factory):
    """Create test environment for CLI integration tests.

    Returns the WT_DIR path that can be used with patch.dict for environment setup.
    CLI tests use this to set up proper configuration without external dependencies.
    """
    # Create repo and config using factories
    repo_path = repo_factory.create_repo()
    factory = config_factory(repo_path)
    config = factory.minimal()

    # Return the WT_DIR path (the .wt directory)
    return config.wt_dir


@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def wt_env(cli_test_env, monkeypatch):
    """Set WT_DIR to the per-test cli_test_env path and return it.

    Use in CLI tests to avoid repeating monkeypatch code.
    """
    monkeypatch.setenv("WT_DIR", str(cli_test_env))
    return cli_test_env


@pytest.fixture
def build_status_response():
    """Factory: build a StatusResponse from a mapping of results.

    Usage:
        # Simple: name as key, StatusResult as value (path derived from name)
        status = build_status_response({"feature": StatusResult(...)})

        # With explicit path: tuple of (StatusResult, Path)
        status = build_status_response({"main": (StatusResult(...), Path("/repo"))})

        empty = build_status_response({})
    """

    def _build(results_dict: dict | None = None) -> StatusResponse:
        results_dict = results_dict or {}
        items = {}
        for name, value in results_dict.items():
            if isinstance(value, tuple):
                status_result, absolute_path = value
            else:
                status_result = value
                absolute_path = Path(f"/tmp/{name}")
            items[name] = StatusItem(
                name=name,
                absolute_path=absolute_path,
                processing_time_ms=10.0,  # Default timing for tests
                result=StatusResultOk(status=status_result),
            )
        return StatusResponse(
            items=items,
            total_processing_time_ms=(sum(it.processing_time_ms for it in items.values()) if items else 0.0),
            daemon_health=DaemonHealth(status=DaemonHealthStatus.OK),
        )

    return _build


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def sample_commit_info():
    """Sample CommitInfo for tests."""
    return CommitInfo(
        hash="abc123def456",
        short_hash="abc123de",
        message="Test commit",
        author="Test Author",
        date="2024-01-15T10:30:00",
    )


@pytest.fixture
def sample_status_result(sample_commit_info):
    """Sample StatusResult for tests."""
    return StatusResult(
        branch_name="test/test-branch",
        upstream_branch="main",
        ahead_count=0,
        behind_count=0,
        pr_info=PRInfoDisabled(),
        commit_info=sample_commit_info,
        last_updated_at=datetime.now(),
        dirty_files_lower_bound=0,
        untracked_files_lower_bound=0,
    )


def assert_worktree_exists(worktree_path: Path, expected_branch: str | None = None):
    assert worktree_path.exists(), f"Worktree {worktree_path} does not exist"
    assert worktree_path.is_dir(), f"Worktree {worktree_path} is not a directory"

    if expected_branch:
        repo = pygit2.Repository(worktree_path)
        head_ref = repo.head.shorthand
        assert head_ref == expected_branch, f"Expected branch {expected_branch}, got {head_ref}"


def assert_worktree_not_exists(worktree_path: Path):
    assert not worktree_path.exists(), f"Worktree {worktree_path} should not exist"


# Integration test fixtures for daemon-based tests
# ================================================


def kill_daemon_at_wt_dir(wt_dir: Path) -> None:
    """Cleanly stop daemon for WT_DIR and assert no leftovers.

    Policy for parallel isolation:
    - Only perform clean shutdown via CLI RPC (no PID signals here)
    - Wait briefly for pid/socket removal
    - If leftovers remain, raise AssertionError to surface leaks early
    """

    pid_file = wt_dir / "daemon.pid"
    sock_file = wt_dir / "daemon.sock"

    # If nothing suggests a running daemon, nothing to do
    if not pid_file.exists() and not sock_file.exists():
        return

    env = os.environ.copy()
    env["WT_DIR"] = str(wt_dir)

    # Attempt graceful shutdown via CLI (succeeds even if daemon already gone)
    try:
        result = run_cli_command(["sh", "kill-daemon"], env=env, timeout=timedelta(seconds=5))
    except Exception as e:
        # Don't attempt any PID-based killing here; surface error
        raise AssertionError(f"kill-daemon invocation failed for {wt_dir}: {e}") from e

    # Wait up to ~1s for files to be removed by daemon shutdown
    removed = wait_until(
        lambda: (not pid_file.exists()) and (not sock_file.exists()), timeout_seconds=1.0, interval_seconds=0.05
    )
    if removed:
        return

    # If still present, declare failure (leak); do not unlink to preserve evidence
    details = (result.stdout or "") + ("\n" + (result.stderr or ""))
    raise AssertionError(
        f"Daemon did not shut down cleanly for {wt_dir}. Leftovers: "
        f"pid_exists={pid_file.exists()} sock_exists={sock_file.exists()}\n{details}"
    )


def create_integration_test_config_file(repo_path: Path) -> Path:
    """Create a test config file for integration tests using centralized helper.

    Creates config in separate WT_DIR to test for baked-in assumptions.
    """
    # Put WT_DIR in separate location to test for baked-in assumptions about WT_DIR = MAIN_REPO/.wt
    temp_parent = repo_path.parent
    wt_dir = temp_parent / "WTDIR" / ".wt"

    # Use centralized helper to create configuration
    build_test_configuration(
        repo_path,
        wt_dir=wt_dir,
        branch_prefix="test/",
        upstream_branch="HEAD",
        log_operations=False,
        cow_method="copy",
        github_enabled=False,
        github_repo="test/test",
    )

    return wt_dir / "config.yaml"


@pytest.fixture(scope="session")
def require_gitstatusd():
    """Fixture that skips test if gitstatusd is not available.

    Checks Bazel runfiles first (hermetic test execution), then PATH.
    Integration tests that need gitstatusd should depend on this fixture.
    Not autouse - unit tests can run without gitstatusd.
    """
    if find_gitstatusd_in_runfiles():
        return
    if shutil.which("gitstatusd"):
        return
    pytest.skip("gitstatusd not available (not in runfiles or PATH)")


@pytest.fixture
def real_temp_repo(repo_factory, require_gitstatusd):
    """Create real temporary git repository for integration tests.

    Uses modern repo_factory internally but maintains compatibility with
    existing integration tests that need real git repositories.
    """
    return repo_factory.create_repo(name="test_repo")


@pytest.fixture
def real_config(real_temp_repo, config_factory) -> Configuration:
    """Create real configuration for integration tests.

    This fixture provides the Configuration object directly for tests
    that need to access config properties like worktrees_dir, main_repo, etc.
    """
    factory = config_factory(real_temp_repo)
    config: Configuration = factory.integration(github_enabled=False)
    return config


@pytest.fixture
def real_env(real_config):
    """Set up real environment for integration tests with proper cleanup.

    Creates environment dict for tests that need to interact with actual
    daemon processes and gitstatusd.

    The hermetic git environment is applied globally by autouse fixture.
    """
    # Ensure clean daemon state for this WT_DIR
    kill_daemon_at_wt_dir(real_config.wt_dir)

    # Set up environment
    env = os.environ.copy()
    env["WT_DIR"] = str(real_config.wt_dir)

    yield env

    # Cleanup: Kill daemon after test
    kill_daemon_at_wt_dir(real_config.wt_dir)


class WtCLI:
    """Convenience wrapper around run_cli_command bound to real_env."""

    def __init__(self, env: dict[str, str]):
        self.env: dict[str, str] = env

    def sh(self, *args: str, timeout: timedelta = timedelta(seconds=30), cwd: Path | None = None, stdin=None):
        return run_cli_command(["sh", *args], env=self.env, timeout=timeout, cwd=cwd, stdin=stdin)

    def sh_c(self, cmd: str, timeout: timedelta = timedelta(seconds=30), cwd: Path | None = None, stdin=None):
        return run_cli_command(["sh", "create", "--yes", cmd], env=self.env, timeout=timeout, cwd=cwd, stdin=stdin)

    def status(self, timeout: timedelta = timedelta(seconds=30), cwd: Path | None = None):
        return run_cli_command(["sh"], env=self.env, timeout=timeout, cwd=cwd)

    def kill(self, timeout: timedelta = timedelta(seconds=30)):
        """Request the daemon to shut down via CLI."""
        return run_cli_command(["sh", "kill-daemon"], env=self.env, timeout=timeout)

    def rpc(self, sock_path: str | os.PathLike, method: str, params: dict):
        """Minimal JSON-RPC helper to call the daemon directly over a UNIX socket."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(sock_path))
            with s.makefile("rwb") as f:
                req = {"jsonrpc": "2.0", "method": method, "params": params, "id": str(uuid.uuid4())}
                f.write((json.dumps(req) + "\n").encode())
                f.flush()
                if line := f.readline():
                    return json.loads(line.decode())
                return {"error": "no response"}

    def wait_for(self, predicate, timeout: timedelta = timedelta(seconds=5), interval: float = 0.1) -> bool:
        """Poll a predicate until it returns True or timeout elapses."""
        return wait_until(predicate, timeout_seconds=timeout.total_seconds(), interval_seconds=interval)


@pytest.fixture
def wt_cli(real_env) -> WtCLI:
    """Fixture returning a typed wrapper bound to the real environment."""

    return WtCLI(real_env)


@pytest.fixture
def wtcli():
    """Factory fixture: wtcli(env) -> WtCLI bound to env."""

    def _make(env: dict[str, str]) -> WtCLI:
        return WtCLI(env)

    return _make


@pytest.fixture
def real_env_with_existing_worktrees(real_temp_repo, config_factory):
    """Set up real environment with pre-created worktrees for complex tests."""
    # Create config using factory pattern
    factory = config_factory(real_temp_repo)
    config = factory.integration(github_enabled=False)

    # Ensure clean daemon state for this WT_DIR before creating worktrees
    kill_daemon_at_wt_dir(config.wt_dir)

    # Create some test worktrees using real worktree service

    git_manager = GitManager(config=config)
    github_mock = Mock()  # GitHub not needed for worktree creation
    worktree_service = WorktreeService(git_manager, github_mock)

    # Create a couple of test worktrees
    worktree_service.create_worktree(config, "existing-1")
    worktree_service.create_worktree(config, "existing-2")

    # Set up environment
    env = os.environ.copy()
    env["WT_DIR"] = str(config.wt_dir)

    yield env

    # Cleanup: Kill daemon after test
    kill_daemon_at_wt_dir(config.wt_dir)


@pytest.fixture
def test_config(repo_factory, config_factory) -> Configuration:
    """Create test configuration for simple unit tests.

    Uses modern factory pattern internally but maintains compatibility
    with existing tests that need basic configuration.
    """
    repo_path = repo_factory.create_repo()
    factory = config_factory(repo_path)
    config: Configuration = factory.minimal(upstream_branch="main")
    return config


def build_test_configuration(repo_path: Path, wt_dir: Path | None = None, **config_overrides) -> Configuration:
    """Centralized helper to build test configurations with the standard pattern.

    This eliminates duplication of the ConfigFile → YAML → Configuration.resolve workflow.
    """
    if wt_dir is None:
        wt_dir = repo_path / ".wt"

    # Default config suitable for most tests
    defaults = {
        "main_repo": str(repo_path),
        "worktrees_dir": str(repo_path / "worktrees"),
        "branch_prefix": "test/",
        "upstream_branch": "main",
        "github_repo": "test-user/test-repo",
        "github_enabled": False,
        "log_operations": True,
        "cache_expiration": 3600,
        "cache_refresh_age": 300,
        "hidden_worktree_patterns": [],
        "gitstatusd_path": None,
        "cow_method": "copy",
    }

    config_file = ConfigFile(**{**defaults, **config_overrides})

    # Save to .wt directory
    wt_dir.mkdir(parents=True, exist_ok=True)
    config_path = wt_dir / "config.yaml"

    config_path.write_text(yaml.dump(config_file.model_dump()), encoding="utf-8")

    return Configuration.resolve(wt_dir)


# Apply hermetic git environment to every test to prevent leakage from user/system config
# Ensures subprocesses inherit HOME/XDG/GIT_* isolation unless a test explicitly overrides
@pytest.fixture(autouse=True)
def _apply_isolated_git_env(tmp_path: Path, monkeypatch):
    """Apply hermetic git environment per test to prevent leakage.

    Sets HOME/XDG_CONFIG_HOME; GIT_* vars are set via pytest config.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def _find_wt_cli_binary() -> str | None:
    """Find the Bazel-built wt-cli binary if running under Bazel."""
    # Check for Bazel runfiles environment
    runfiles_dir = os.environ.get("RUNFILES_DIR") or os.environ.get("TEST_SRCDIR")
    if not runfiles_dir:
        return None

    # Try to find wt-cli in runfiles (it's in the same repo)
    # The path structure is: runfiles/_main/wt/wt-cli
    candidate = Path(runfiles_dir) / "_main" / "wt" / "wt-cli"
    if candidate.exists():
        return str(candidate)

    return None


def _generate_wt_function_for_binary(binary_path: str) -> str:
    """Generate shell function using a specific binary path.

    Reads the template from wt/shell/wt.sh and substitutes the binary path,
    matching how install.main() works but using the Bazel binary instead of python -m.
    """
    quoted_path = shlex.quote(binary_path)
    with importlib.resources.files("wt.shell").joinpath("wt.sh").open("r", encoding="utf-8") as f:
        tpl = f.read()
    # The template uses __PY__ -m wt.cli sh, replace with direct binary call
    return tpl.replace("__PY__ -m wt.cli sh", quoted_path + " sh")


@pytest.fixture
def shell_runner(tmp_path: Path):
    """Factory for running shell commands via the installed wt shell function."""

    class ShellRunner:
        def run_script(self, script_content: str, *, cwd: Path, env: dict[str, str] | None = None):
            # Ensure wt is importable (package installed) before attempting shell integration
            assert importlib.util.find_spec("wt"), "wt package not installed - required for shell integration tests"
            # Ensure env is a copy
            env = os.environ.copy() if env is None else env.copy()

            # Find the Bazel-built wt-cli binary - tests require it for proper environment setup
            wt_cli_path = _find_wt_cli_binary()
            if not wt_cli_path:
                raise RuntimeError(
                    "wt-cli binary not found in runfiles. Ensure //wt:wt-cli is in the test's data dependencies."
                )
            wt_fn = _generate_wt_function_for_binary(wt_cli_path)

            full_script = f"""#!/bin/bash
# Install wt function via builtin
{wt_fn}

# Original script content
{script_content}
"""
            script_path = tmp_path / f"script_{uuid.uuid4().hex}.sh"
            script_path.write_text(full_script, encoding="utf-8")
            script_path.chmod(0o755)
            return subprocess.run(
                ["/bin/bash", str(script_path)], capture_output=True, text=True, cwd=str(cwd), env=env, check=False
            )

        def run_argv(self, *, cwd: Path, argv: list[str], env: dict[str, str] | None = None):
            return self.run_script(shlex.join(argv), cwd=cwd, env=env)

        def run_wt(self, *, main_repo: Path, wt_args: list[str], env: dict[str, str] | None = None):
            return self.run_argv(cwd=main_repo, argv=["wt", *wt_args], env=env)

    return ShellRunner()
