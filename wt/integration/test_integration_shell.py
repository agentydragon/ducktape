"""
Integration tests for shell function interaction with the CLI.

Tests the shell function installed via `python -m wt.shell.install` that users interact with, including fd3 redirection,
exit code semantics, and process boundary interactions.
"""

# === CRITICAL DEBUGGING WISDOM FOR SHELL TESTING ===
# Testing shell integration is complex due to process boundaries and fd3 redirection.
# Key insights:
# - Use the actual wt shell function that users interact with
# - Test the wt shell function *AND* the Python binary TOGETHER as a system, as they're used by user in shell
# - Test exit codes: 0=success, 1=unhandled error (no fd3), 2=managed error (with fd3)
# - Assumes wt package is properly installed
# - click.echo() outputs to stdout by default, not stderr
# - DON'T mock across process boundaries - create real error conditions instead

from pathlib import Path

import pytest
import pytest_bazel

# Global constants for paths
PROJECT_ROOT = Path(__file__).parent.parent.parent


## tests use shell_runner; legacy helper removed


@pytest.mark.integration
@pytest.mark.shell
class TestShellIntegration:
    def test_help_command_basic(self, test_config, shell_runner):
        """Test that help command works through shell integration."""
        # Test basic help command - should not require real git repo setup
        result = shell_runner.run_wt(main_repo=test_config.main_repo, wt_args=["--help"])

        # Should succeed and show help output
        assert result.returncode == 0, f"Help command failed: {result.stderr}"
        # Click default help prints 'Usage:' for subcommands
        assert "Usage:" in result.stdout

    def test_shell_script_execution_basic(self, test_config, shell_runner):
        """Test that shell script can execute basic wt commands."""
        test_script = """# Test that wt function is available
type wt
echo "Shell function loaded successfully"
"""

        result = shell_runner.run_script(test_script, cwd=test_config.main_repo)

        # Should be able to source the function
        assert result.returncode == 0, f"Shell setup failed: {result.stderr}"
        assert "Shell function loaded successfully" in result.stdout

    def test_successful_teleport_with_pwd_verification(self, real_temp_repo, real_env, shell_runner):
        """Test that wt teleport actually changes directory using pwd verification."""

        # Cleaned by real_env fixture

        def parse_teleport_output(result):
            output_lines = [line for line in result.stdout.strip().split("\n") if line]
            if not output_lines:
                pytest.fail(f"No output from script. Stderr: {result.stderr}")

            output_line = output_lines[-1]
            parts = output_line.split(":", 3)

            if len(parts) != 4:
                pytest.fail(f"Expected 4 parts in output, got {len(parts)}. Output: {output_line}")

            return {
                "create_exit": int(parts[0]),
                "nav_exit": int(parts[1]),
                "pwd_before": parts[2],
                "pwd_after": parts[3],
            }

        # Main test logic
        shell_script = """# Verify shell function is loaded
if ! declare -f wt > /dev/null; then
    echo "ERROR: wt function not loaded"
    exit 99
fi

# Use shell function - it calls Python CLI with fd3 redirection
wt create --yes teleport-test
create_exit=$?

pwd_before=$(pwd)
wt teleport-test
nav_exit=$?
pwd_after=$(pwd)

echo "$create_exit:$nav_exit:$pwd_before:$pwd_after"
"""

        result = shell_runner.run_script(shell_script, cwd=real_temp_repo, env=real_env)

        data = parse_teleport_output(result)

        assert data["create_exit"] == 0, f"Create failed: stdout={result.stdout}, stderr={result.stderr}"
        assert data["nav_exit"] == 0, f"Navigate failed: {result.stderr}"

        expected_dir = str(real_temp_repo / "worktrees" / "teleport-test")
        assert data["pwd_after"] == expected_dir, (
            f"Directory change failed. Expected: {expected_dir}, Got: {data['pwd_after']}"
        )

        worktree_path = real_temp_repo / "worktrees" / "teleport-test"
        assert worktree_path.exists()
        assert worktree_path.is_dir()

    def test_wt_main_changes_directory(self, real_temp_repo, real_env, shell_runner):
        # Cleaned by real_env fixture

        def parse_output(result):
            lines = [line for line in result.stdout.strip().split("\n") if line]
            s = lines[-1]
            parts = s.split(":", 4)
            if len(parts) != 5:
                pytest.fail(f"Bad output: {s}")
            return int(parts[0]), int(parts[1]), int(parts[2]), parts[3], parts[4]

        shell_script = """# Verify shell function is loaded
if ! declare -f wt > /dev/null; then
    echo "ERROR: wt function not loaded"
    exit 99
fi
wt create --yes to-main
create_exit=$?
wt to-main
to_wt_exit=$?
pwd_before=$(pwd)
wt main
to_main_exit=$?
pwd_after=$(pwd)
echo "$create_exit:$to_wt_exit:$to_main_exit:$pwd_before:$pwd_after"
"""

        result = shell_runner.run_script(shell_script, cwd=real_temp_repo, env=real_env)
        c, e1, e2, before, after = parse_output(result)
        assert c == 0, f"Create failed: stdout={result.stdout}, stderr={result.stderr}"
        assert e1 == 0, f"Navigate to worktree failed: {result.stderr}"
        assert e2 == 0, f"Navigate to main failed: {result.stderr}"
        expected_before = str(real_temp_repo / "worktrees" / "to-main")
        assert before == expected_before
        assert after == str(real_temp_repo)


@pytest.mark.integration
@pytest.mark.shell
class TestShellIntegrationEdgeCases:
    def test_shell_environment_isolation(self, test_config, shell_runner):
        """Test that shell environment is properly isolated."""
        # Basic environment test
        env_test_script = """echo "Environment test completed"
"""

        result = shell_runner.run_script(env_test_script, cwd=test_config.main_repo)
        assert result.returncode == 0
        assert "Environment test completed" in result.stdout


if __name__ == "__main__":
    pytest_bazel.main()
