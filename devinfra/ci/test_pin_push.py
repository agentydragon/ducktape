"""Exercise the actual workflow's commit/push against local Git repositories."""

import os
import subprocess
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from python.runfiles import Runfiles


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.mark.parametrize("concurrent_update", [False, True])
def test_sync_push_rejects_stale_source_and_noop_does_not_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, concurrent_update: bool
) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "--initial-branch=devel")
    writer = tmp_path / "writer"
    git(origin, "clone", str(origin), str(writer))
    git(writer, "config", "user.name", "Test")
    git(writer, "config", "user.email", "test@example.invalid")
    (writer / "nix").mkdir()
    (writer / "nix/artifact-pins.json").write_text("old pin\n")
    git(writer, "add", ".")
    git(writer, "commit", "-m", "base")
    git(writer, "push", "origin", "devel")
    runner = tmp_path / "runner"
    git(origin, "clone", str(origin), str(runner))
    # Route the workflow's literal GitHub push URL to this isolated local origin.
    git(runner, "config", f"url.{origin}.insteadOf", "https://x-access-token:test@github.com/test/repo.git")
    base = git(runner, "rev-parse", "HEAD")
    if concurrent_update:
        (writer / "source.py").write_text("new source\n")
        git(writer, "add", ".")
        git(writer, "commit", "-m", "source advances after sync decision")
        git(writer, "push", "origin", "devel")
    remote_before = git(origin, "rev-parse", "devel")
    (runner / "nix/artifact-pins.json").write_text("selected for checked-out source\n")
    workflow = yaml.safe_load(
        (Runfiles.CreateOrRaise().root() / "ducktape/.github/workflows/sync-pins.yml").read_text()
    )
    script = workflow["jobs"]["sync"]["steps"][-1]["run"].replace("${{ steps.sync.outputs.updated }}", "example")
    env = os.environ | {"APP_TOKEN": "test", "APP_SLUG": "test", "APP_USER_ID": "1", "GITHUB_REPOSITORY": "test/repo"}
    result = subprocess.run(
        ["bash", "-e", "-c", script], cwd=runner, env=env, capture_output=True, text=True, check=False
    )
    assert git(runner, "rev-parse", "HEAD^") == base  # The decision was never rebased.
    if concurrent_update:
        assert result.returncode != 0
        assert git(origin, "rev-parse", "devel") == remote_before
        assert "rejected" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert git(origin, "rev-parse", "devel") == git(runner, "rev-parse", "HEAD")
        promoted = git(runner, "rev-parse", "HEAD")
        result = subprocess.run(
            ["bash", "-e", "-c", script], cwd=runner, env=env, capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        assert git(runner, "rev-parse", "HEAD") == promoted
        assert "No pin changes" in result.stdout


if __name__ == "__main__":
    pytest_bazel.main()
