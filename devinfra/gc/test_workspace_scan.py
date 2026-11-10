from pathlib import Path

import pytest
import pytest_bazel

from devinfra.gc import output_base_gc, workspace_scan
from devinfra.gc.conftest import GitRepo, make_base
from devinfra.gc.output_base_gc import RetainedBase, RetainedBaseReason


def test_annotate_base_flags_only_a_prunable_worktree_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    item = RetainedBase(tmp_path / "base", workspace, RetainedBaseReason.WORKSPACE_EXISTS, 0, False)

    prunable = workspace_scan.annotate_base(item, {workspace})
    kept = workspace_scan.annotate_base(item, set())
    assert isinstance(prunable, RetainedBase)
    assert isinstance(kept, RetainedBase)
    assert prunable.workspace_is_prunable_worktree
    assert not kept.workspace_is_prunable_worktree


def test_base_worktrees_covers_only_retained_base_workspaces(
    repo: GitRepo, proc: Path, mountinfo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(output_base_gc, "_TMP_ROOT", tmp_path / "elsewhere")
    workspace = repo.worktree("wt_workspace", "has_base").path
    repo.worktree("wt_unrelated", "no_base")
    root = tmp_path / "output"
    make_base(root, workspace.resolve())

    bases = list(output_base_gc.scan_output_user_root(root, session_bases=(), proc_root=proc, mountinfo_path=mountinfo))

    assert {worktree.branch for worktree in workspace_scan.base_worktrees(repo.path, bases)} == {"has_base"}


if __name__ == "__main__":
    pytest_bazel.main()
