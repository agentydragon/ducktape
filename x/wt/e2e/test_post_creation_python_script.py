import os
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_bazel

from util.bazel.runfiles import get_required_path
from x.wt.testing.conftest import kill_daemon_at_wt_dir


@pytest.fixture
def real_env_with_python_post_script(real_temp_repo, daemon_config_factory, tmp_path):
    """Provide a real repo + WT_DIR configured to run a Python post-create hook.

    The hook verifies stdin is valid, parses required args, emits stdout/stderr,
    and writes a marker file in the new worktree root.
    """
    script = tmp_path / "post_create.py"
    script.write_text(get_required_path("_main/x/wt/testing/post_create.py").read_text(), encoding="utf-8")
    script.chmod(0o755)

    # Configure environment (WT_DIR) with this post-creation script
    factory = daemon_config_factory(real_temp_repo)
    config = factory.integration(github_repo=None, post_creation_script=str(script))
    env = os.environ.copy()
    env["WT_DIR"] = str(config.wt_dir)

    # Ensure a clean daemon state for this WT_DIR
    kill_daemon_at_wt_dir(config.wt_dir)
    try:
        yield env, real_temp_repo
    finally:
        kill_daemon_at_wt_dir(config.wt_dir)


def test_post_creation_python_script_runs(real_env_with_python_post_script, wtcli):
    env, repo = real_env_with_python_post_script
    name = "py-hooked"

    # Run CLI in "sh -c <name>" mode which triggers creation and post-create hook
    result = wtcli(env).sh_c(name, timeout=timedelta(seconds=30.0))
    assert result.returncode == 0, (
        f"wt create failed: rc={result.returncode}\nstdout=\n{result.stdout}\n\nstderr=\n{result.stderr}"
    )

    wt_path = Path(repo) / "worktrees" / name
    assert wt_path.exists(), f"missing worktree at {wt_path}"
    assert wt_path.is_dir(), f"worktree path is not a directory: {wt_path}"
    assert (wt_path / ".py_post_create_ran").exists(), ".py_post_create_ran not created by python post-create"
    assert "Fatal Python error: init_sys_streams" not in (result.stdout or "") + (result.stderr or "")


if __name__ == "__main__":
    pytest_bazel.main()
