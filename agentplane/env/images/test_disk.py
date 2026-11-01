"""The environment disk supplies each independently runnable process and the CLI."""

import os
import subprocess
from pathlib import Path

import pytest_bazel

from util.bazel.runfiles import get_required_path


def test_environment_disk_holds_the_executables(tmp_path: Path) -> None:
    subprocess.run(
        [
            get_required_path("_main/devinfra/erofs/erofs_tool_bin"),
            get_required_path(os.environ["EROFS_CLOSURE"]),
            "fsck.erofs",
            f"--extract={tmp_path}",
            "--preserve-perms",
            get_required_path("_main/agentplane/env/images/runner.img"),
        ],
        check=True,
    )

    for name in (
        "agentplane",
        "agentplane-environment-runtime",
        "agentplane-execution-service",
        "agentplane-harness-host",
    ):
        assert os.access(tmp_path / "bin" / name, os.X_OK)


if __name__ == "__main__":
    pytest_bazel.main()
