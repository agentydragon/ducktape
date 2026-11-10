"""Preserve the VM home while moving it from the workspace PVC to state."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
import pytest_bazel
from python.runfiles import Runfiles

HOME_PREPARE = Runfiles.CreateOrRaise().root() / "ducktape/agentplane/env/images/home_prepare"


def _prepare(legacy_home: Path, agent_home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", HOME_PREPARE, str(legacy_home), str(agent_home)], check=False, capture_output=True, text=True
    )


def test_home_prepare_moves_existing_home_and_preserves_file_metadata(tmp_path: Path) -> None:
    legacy_home = tmp_path / "workspace" / ".home"
    agent_home = tmp_path / "state" / "home"
    agent_home.parent.mkdir()
    legacy_home.mkdir(parents=True, mode=0o700)
    user_file = legacy_home / "settings"
    user_file.write_text("preserve this file\n")
    user_file.chmod(0o640)

    result = _prepare(legacy_home, agent_home)

    assert result.returncode == 0, result.stderr
    assert not legacy_home.exists()
    assert (agent_home / "settings").read_text() == "preserve this file\n"
    assert stat.S_IMODE((agent_home / "settings").stat().st_mode) == 0o640
    assert stat.S_IMODE(agent_home.stat().st_mode) == 0o700
    assert (agent_home.stat().st_uid, agent_home.stat().st_gid) == (os.getuid(), os.getgid())

    result = _prepare(legacy_home, agent_home)
    assert result.returncode == 0, result.stderr
    assert (agent_home / "settings").read_text() == "preserve this file\n"


def test_home_prepare_creates_fresh_home_and_preserves_it_on_reboot(tmp_path: Path) -> None:
    legacy_home = tmp_path / "workspace" / ".home"
    agent_home = tmp_path / "state" / "home"
    legacy_home.parent.mkdir()
    agent_home.parent.mkdir()

    result = _prepare(legacy_home, agent_home)

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(agent_home.stat().st_mode) == 0o700
    assert (agent_home.stat().st_uid, agent_home.stat().st_gid) == (os.getuid(), os.getgid())
    (agent_home / "user-state").write_text("retain across restart\n")

    result = _prepare(legacy_home, agent_home)

    assert result.returncode == 0, result.stderr
    assert (agent_home / "user-state").read_text() == "retain across restart\n"


def test_home_prepare_refuses_to_merge_existing_home_paths(tmp_path: Path) -> None:
    legacy_home = tmp_path / "workspace" / ".home"
    agent_home = tmp_path / "state" / "home"
    legacy_home.mkdir(parents=True)
    agent_home.mkdir(parents=True)
    (legacy_home / "legacy").write_text("legacy\n")
    (agent_home / "canonical").write_text("canonical\n")

    result = _prepare(legacy_home, agent_home)

    assert result.returncode != 0
    assert (legacy_home / "legacy").read_text() == "legacy\n"
    assert (agent_home / "canonical").read_text() == "canonical\n"


@pytest.mark.parametrize("invalid_path", ["legacy-file", "legacy-symlink", "home-file", "home-symlink"])
def test_home_prepare_refuses_non_directory_and_symlink_paths(tmp_path: Path, invalid_path: str) -> None:
    legacy_home = tmp_path / "workspace" / ".home"
    agent_home = tmp_path / "state" / "home"
    legacy_home.parent.mkdir()
    agent_home.parent.mkdir()
    if invalid_path.startswith("legacy"):
        if invalid_path == "legacy-file":
            legacy_home.write_text("keep\n")
        else:
            outside = tmp_path / "outside"
            outside.mkdir()
            legacy_home.symlink_to(outside, target_is_directory=True)
    elif invalid_path == "home-file":
        agent_home.write_text("keep\n")
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        agent_home.symlink_to(outside, target_is_directory=True)

    result = _prepare(legacy_home, agent_home)

    assert result.returncode != 0
    if invalid_path.endswith("file"):
        path = legacy_home if invalid_path.startswith("legacy") else agent_home
        assert path.read_text() == "keep\n"
    else:
        path = legacy_home if invalid_path.startswith("legacy") else agent_home
        assert path.is_symlink()


if __name__ == "__main__":
    pytest_bazel.main()
