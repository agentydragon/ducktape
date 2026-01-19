import os
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_bazel

from wt.testing.conftest import kill_daemon_at_wt_dir


@pytest.fixture
def real_env_with_python_post_script(real_temp_repo, config_factory, tmp_path):
    """Provide a real repo + WT_DIR configured to run a Python post-create hook.

    The hook verifies stdin is valid, parses required args, emits stdout/stderr,
    and writes a marker file in the new worktree root.
    """
    # Create Python post-create script
    script = tmp_path / "post_create.py"
    script.write_text(
        """#!/usr/bin/env python3
import sys, argparse
from pathlib import Path
from datetime import timedelta
import pytest_bazel

# Verify stdin is valid (raises if fd 0 is bad)
sys.stdin.fileno()

p = argparse.ArgumentParser()
p.add_argument("--worktree_root", required=True)
p.add_argument("--worktree_name", required=True)
args, _ = p.parse_known_args()

root = Path(args.worktree_root)
if not root.exists():
    print("worktree root does not exist", file=sys.stderr)
    sys.exit(3)

print("py post-create: hello from stdout")
print("py post-create: hello from stderr", file=sys.stderr)

(root / ".py_post_create_ran").write_text("ok")
""",
        encoding="utf-8",
    )
    script.chmod(0o755)

    # Configure environment (WT_DIR) with this post-creation script
    factory = config_factory(real_temp_repo)
    config = factory.integration(github_enabled=False, post_creation_script=str(script))
    env = os.environ.copy()
    env["WT_DIR"] = str(config.wt_dir)

    # Ensure a clean daemon state for this WT_DIR
    kill_daemon_at_wt_dir(config.wt_dir)
    try:
        yield env, real_temp_repo
    finally:
        kill_daemon_at_wt_dir(config.wt_dir)


@pytest.mark.parametrize("stdin_mode", ["open", "closed"])
def test_post_creation_python_script_runs(real_env_with_python_post_script, stdin_mode, wtcli):
    env, repo = real_env_with_python_post_script
    name = "py-hooked"

    # Run CLI in "sh -c <name>" mode which triggers creation and post-create hook
    # Choose stdin behavior: open (default) vs closed (simulate bad fd 0 for daemon)
    stdin = None if stdin_mode == "open" else subprocess.DEVNULL  # parent CLI stdin is /dev/null; daemon inherits this

    cli = wtcli(env)
    result = cli.sh_c(name, timeout=timedelta(seconds=30.0), stdin=stdin)

    if stdin_mode == "open":
        assert result.returncode == 0, (
            f"wt create failed: rc={result.returncode}\nstdout=\n{result.stdout}\n\nstderr=\n{result.stderr}"
        )
    else:
        # Using /dev/null for stdin is a valid fd (not truly "closed").
        # Current behavior: hook inherits a valid stdin, so it should succeed too.
        assert result.returncode == 0, (
            "expected success with stdin=/dev/null; got rc="
            f"{result.returncode}\nstdout=\n{result.stdout}\n\nstderr=\n{result.stderr}"
        )
        # Keep outputs for visibility
        combined = (result.stdout or "") + (result.stderr or "")
        assert "Fatal Python error" not in combined

    wt_path = Path(repo) / "worktrees" / name
    assert wt_path.exists(), f"missing worktree at {wt_path}"
    assert wt_path.is_dir(), f"worktree path is not a directory: {wt_path}"
    marker = wt_path / ".py_post_create_ran"
    if stdin_mode == "open":
        assert marker.exists(), ".py_post_create_ran not created by python post-create"
        combined = (result.stdout or "") + (result.stderr or "")
        assert "Fatal Python error: init_sys_streams" not in combined
    else:
        # Closed mode (/dev/null) also succeeds; marker should still be present.
        assert marker.exists(), ".py_post_create_ran not created in closed-stdin mode"


if __name__ == "__main__":
    pytest_bazel.main()
