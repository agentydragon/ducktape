"""The runner disk holds what the VM guest and a runner Pod execute.

Neither runs in CI, so this pins the paths they start: the guest's systemd unit runs
`bin/agentplane-runner-vm` and a Pod runs `bin/agentplane-runner`.
"""

import os
import subprocess
from pathlib import Path

import pytest_bazel

from util.bazel.runfiles import get_required_path


def test_runner_disk_holds_the_executables(tmp_path: Path) -> None:
    subprocess.run(
        [
            get_required_path("_main/devinfra/erofs/erofs_tool_bin"),
            get_required_path(os.environ["EROFS_CLOSURE"]),
            "fsck.erofs",
            f"--extract={tmp_path}",
            "--preserve-perms",
            get_required_path("_main/agentplane/runner/runner.img"),
        ],
        check=True,
    )

    for name in ("agentplane", "agentplane-runner"):
        assert os.access(tmp_path / "bin" / name, os.X_OK)
    assert (tmp_path / "bin/agentplane-runner-vm").readlink() == Path("agentplane-runner")


if __name__ == "__main__":
    pytest_bazel.main()
