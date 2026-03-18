"""E2E tests for session_start hook with MockEgressProxy.

Unit tests are in test_session_start_input.py.
Podman integration tests are in test_session_start_podman.py (requires local=True).
"""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.claude.claude_api.hooks.session_start import HookSource
from devinfra.claude.testing import shell_helpers
from devinfra.claude.testing.fixtures import MockEgressProxyFixture, collect_supervisor_logs
from devinfra.claude.testing.session_start_helpers import IsolatedDirs, run_session_start_hook, setup_hook_env
from util.testing.undeclared_outputs import undeclared_outputs_dir

# Register fixtures from modules (pytest-native, no direct name import needed)
pytest_plugins = ["devinfra.claude.testing.fixtures", "devinfra.claude.testing.session_start_helpers"]


@pytest.fixture
def hook_env(
    monkeypatch: pytest.MonkeyPatch, isolated_dirs: IsolatedDirs, mock_egress_proxy: MockEgressProxyFixture
) -> None:
    """Set up environment for running the session start hook (container runtime disabled)."""
    setup_hook_env(monkeypatch, isolated_dirs, mock_egress_proxy.proxy, container_runtime="none")


def _save_bazel_logs(result: subprocess.CompletedProcess[str], label: str) -> None:
    """Save bazel invocation stdout/stderr to undeclared test outputs."""
    out_dir = undeclared_outputs_dir() / f"bazel-{label}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if result.stdout:
        (out_dir / "stdout.log").write_text(result.stdout)
    if result.stderr:
        (out_dir / "stderr.log").write_text(result.stderr)


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
        test_file_dir = Path(__file__).parent.parent
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
                    command=(f"bazel --output_base={output_base} build --shell_executable=$(which bash) //:hello"),
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


if __name__ == "__main__":
    pytest_bazel.main()
