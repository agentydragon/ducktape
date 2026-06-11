import os
from datetime import timedelta
from pathlib import Path

import pytest

from wt.testing.conftest import kill_daemon_at_wt_dir


@pytest.fixture
def real_env_with_post_script(real_temp_repo, config_factory, tmp_path):
    script = tmp_path / "post_create.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'wt=""\n'
        'for a in "$@"; do case "$a" in --worktree_root=*) wt="${a#*=}";; esac; done\n'
        'if [[ -z "$wt" ]]; then echo "missing --worktree_root" >&2; exit 2; fi\n'
        'touch "$wt/.post_create_ran"\n'
    )
    script.chmod(0o755)
    factory = config_factory(real_temp_repo)
    config = factory.integration(github_enabled=False, post_creation_script=str(script))
    env = os.environ.copy()
    env["WT_DIR"] = str(config.wt_dir)
    # Ensure clean daemon for this WT_DIR
    kill_daemon_at_wt_dir(config.wt_dir)
    yield env, real_temp_repo
    kill_daemon_at_wt_dir(config.wt_dir)


def test_post_creation_script_runs(real_env_with_post_script, wtcli):
    env, repo = real_env_with_post_script
    name = "hooked"
    result = wtcli(env).sh_c(name, timeout=timedelta(seconds=15.0))
    assert result.returncode == 0
    wt_path = Path(repo) / "worktrees" / name
    assert wt_path.exists()
    assert (wt_path / ".post_create_ran").exists()
