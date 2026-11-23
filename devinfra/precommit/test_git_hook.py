"""Tests for Git hook tracing in ordinary checkouts and linked worktrees."""

from __future__ import annotations

import json
from pathlib import Path

import pygit2
import pytest
import pytest_bazel
from opentelemetry.sdk.trace import TracerProvider

from devinfra.precommit import git_hook
from devinfra.precommit.conftest import commit, stage


@pytest.mark.parametrize("linked", [False, True], ids=["checkout", "worktree"])
@pytest.mark.parametrize("invalid", [False, True], ids=["valid", "invalid"])
def test_pre_commit_reports_checks_and_writes_traces(
    repo: pygit2.Repository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    request: pytest.FixtureRequest,
    linked: bool,
    invalid: bool,
) -> None:
    git_dir = Path(repo.workdir) / ".git"
    specimen = "props/specimens/sample"
    frozen_code = f"{specimen}/code/file.py"
    stage(repo, f"{specimen}/issues/example.yml")
    stage(repo, frozen_code)
    commit(repo)
    if linked:
        workspace = tmp_path / "wt"
        repo.add_worktree("wt", str(workspace))
        repo = pygit2.Repository(str(workspace))
        git_dir = git_dir / "worktrees" / "wt"

    if invalid:
        stage(repo, "bad-name.py")
        stage(repo, frozen_code, "# changed")
    else:
        stage(repo, "good_name.py")

    # The global OpenTelemetry provider is set only once per process.
    def set_provider(provider: TracerProvider) -> None:
        monkeypatch.setattr(git_hook, "tracer", provider.get_tracer(git_hook.__name__))
        request.addfinalizer(provider.shutdown)

    monkeypatch.setattr(git_hook.trace, "set_tracer_provider", set_provider)
    monkeypatch.chdir(repo.workdir)
    assert git_hook.main_pre_commit() == int(invalid)

    output = capsys.readouterr()
    checks = ("filename-conventions", "frozen-specimens")
    for check in checks:
        assert f"{check}: {'FAILED' if invalid else 'ok'}" in output.out
    if invalid:
        assert "bad-name.py" in output.err
        assert frozen_code in output.err
    else:
        assert not output.err

    records = {
        record["name"]: record["status"]["status_code"]
        for record in map(json.loads, (git_dir / "precommit-traces.jsonl").read_text().splitlines())
    }
    assert records.keys() == {*checks, "precommit"}
    for check in checks:
        assert records[check] == ("ERROR" if invalid else "UNSET")


if __name__ == "__main__":
    pytest_bazel.main()
