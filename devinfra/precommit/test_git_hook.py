"""Tests for Git hook tracing in ordinary checkouts and linked worktrees."""

from __future__ import annotations

import json
from pathlib import Path

import pygit2
import pytest
from opentelemetry.sdk.trace import TracerProvider

from devinfra.precommit import git_hook
from devinfra.precommit.conftest import commit, stage


@pytest.mark.parametrize("linked", [False, True], ids=["checkout", "worktree"])
def test_traces_are_written_to_git_dir(
    repo: pygit2.Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked: bool
) -> None:
    git_dir = Path(repo.workdir) / ".git"
    if linked:
        stage(repo, "file.txt")
        commit(repo)
        workspace = tmp_path / "wt"
        repo.add_worktree("wt", str(workspace))
        repo = pygit2.Repository(str(workspace))
        git_dir = git_dir / "worktrees" / "wt"

    # The global OpenTelemetry provider is set only once per process.
    providers: list[TracerProvider] = []
    monkeypatch.setattr(git_hook.trace, "set_tracer_provider", providers.append)
    git_hook._setup_tracing(repo)

    (provider,) = providers
    with provider.get_tracer(__name__).start_as_current_span("hook-test"):
        pass
    provider.shutdown()

    record = json.loads((git_dir / "precommit-traces.jsonl").read_text())
    assert record["name"] == "hook-test"
