"""
Integration tests for shell function interaction with the CLI.

Tests the shell function installed via `python -m x.wt.shell.install` that users interact with, including fd3 redirection,
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

import pytest
import pytest_bazel

# The test drives `python -m x.wt.shell.install` in a subprocess; the module
# is referenced only as a string, so gazelle cannot see the dependency.
# gazelle:include_dep //x/wt/shell:install


class TestShellIntegration:
    def test_help_command_basic(self, test_config, shell_runner):
        """Test that help command works through shell integration."""
        # Test basic help command - should not require real git repo setup
        result = shell_runner.run_wt(main_repo=test_config.main_repo, wt_args=["--help"])

        # Should succeed and show help output
        assert result.returncode == 0, f"Help command failed: {result.stderr}"
        # Click default help prints 'Usage:' for subcommands
        assert "Usage:" in result.stdout

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


if __name__ == "__main__":
    pytest_bazel.main()
