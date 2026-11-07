"""The environment disk supplies each independently runnable process and the CLI."""

import os
import subprocess
from pathlib import Path

import pytest_bazel
from python.runfiles import Runfiles


def test_environment_disk_holds_the_executables(tmp_path: Path) -> None:
    runfiles_root = Runfiles.CreateOrRaise().root()
    subprocess.run(
        [
            (runfiles_root / "ducktape/devinfra/erofs/erofs_tool_bin"),
            (runfiles_root / os.environ["EROFS_CLOSURE"]),
            "fsck.erofs",
            f"--extract={tmp_path}",
            "--preserve-perms",
            (runfiles_root / "ducktape/agentplane/env/images/runner.img"),
        ],
        check=True,
    )

    for name in (
        "agentplane",
        "agentplane-environment-runtime",
        "agentplane-execution-service",
        "agentplane-firecracker-thaw-agent",
    ):
        assert os.access(tmp_path / "bin" / name, os.X_OK)


if __name__ == "__main__":
    pytest_bazel.main()
