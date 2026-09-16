import os
import shutil
import time
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.gc import git_repo, worktree_gc as wg
from devinfra.gc.conftest import GitRepo
from devinfra.gc.pull_request import PrInfo, PrState


def _classify(repo: GitRepo, path: Path, proc: Path, **kwargs: object) -> wg.Classification:
    kwargs.setdefault("pr_states", {})
    kwargs.setdefault("active_path", None)
    main_path = git_repo.main_worktree(repo.path)
    linked = [wt for wt in git_repo.list_worktrees(repo.path) if wt.path != main_path]
    live = wg.processes_by_worktree((wt.path for wt in linked), proc_root=proc)
    worktree = next(wt for wt in linked if wt.path == path)
    return wg.classify_worktree(worktree, main="main", main_path=main_path, live_pids=live.get(path, []), **kwargs)  # type: ignore[arg-type]


def test_ancestor_is_prunable(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")  # branched at main's HEAD, then main moves ahead
    repo.commit("later", "1\n", "advance main")
    result = _classify(repo, wt.path, proc)
    assert isinstance(result, wg.PrunableWorktree)
    assert "already in main" in result.reason


def test_squash_merge_is_prunable(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")
    wt.commit("shared", "same\n", "add on branch")
    repo.commit("shared", "same\n", "same change squashed onto main")
    # Merging the branch into main is now a no-op — its content is already there.
    assert isinstance(_classify(repo, wt.path, proc), wg.PrunableWorktree)


def test_empty_branch_is_prunable(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")  # no commits beyond main
    assert isinstance(_classify(repo, wt.path, proc), wg.PrunableWorktree)


def test_unique_unmerged_is_review(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")
    wt.commit("novel", "unique\n", "unmerged work")
    result = _classify(repo, wt.path, proc)
    assert isinstance(result, wg.ReviewWorktree)
    assert "no merged PR" in result.reason


def test_dirty_tracked_change_is_kept(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")
    (wt.path / "base").write_text("dirty\n")
    result = _classify(repo, wt.path, proc)
    assert isinstance(result, wg.RetainedWorktree)
    assert result.reason == "uncommitted changes"


def test_untracked_file_is_kept(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")
    (wt.path / "scratch").write_text("x\n")
    assert isinstance(_classify(repo, wt.path, proc), wg.RetainedWorktree)


def test_dirty_with_open_pr_notes_the_pr(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")
    (wt.path / "base").write_text("dirty\n")
    result = _classify(repo, wt.path, proc, pr_states={"feature": PrInfo(9, PrState.OPEN)})
    assert isinstance(result, wg.RetainedWorktree)
    assert result.reason == "uncommitted changes (open PR #9)"


def test_dirty_with_merged_pr_is_kept_and_flagged(repo: GitRepo, proc: Path) -> None:
    # Uncommitted work always wins over the merged-PR prune, but the reason surfaces the
    # merge so the tree reads as stale scratch worth clearing by hand.
    wt = repo.worktree("wt", "feature")
    (wt.path / "base").write_text("dirty\n")
    result = _classify(repo, wt.path, proc, pr_states={"feature": PrInfo(5, PrState.MERGED)})
    assert isinstance(result, wg.RetainedWorktree)
    assert result.reason == "uncommitted changes (PR #5 merged)"


def test_last_activity_reflects_uncommitted_file_mtime(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")
    edited = wt.path / "base"
    edited.write_text("dirty\n")
    future = time.time() + 10_000  # newer than the HEAD commit
    os.utime(edited, (future, future))
    result = _classify(repo, wt.path, proc)
    assert result.last_activity is not None
    assert result.last_activity.timestamp() == pytest.approx(future, abs=2)


def test_missing_worktree_directory_is_prunable(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")
    shutil.rmtree(wt.path)  # `git worktree list` still reports it, marked prunable
    result = _classify(repo, wt.path, proc)
    assert isinstance(result, wg.PrunableWorktree)
    assert result.reason == "worktree directory is missing"
    assert result.last_activity is None


def test_detached_head_with_commit_is_review(repo: GitRepo, proc: Path) -> None:
    path = repo.path.parent / "wt"
    repo.run("worktree", "add", "-q", "--detach", str(path), "main")
    GitRepo(path).commit("novel", "unique\n", "detached work")
    result = _classify(repo, path, proc)
    assert isinstance(result, wg.ReviewWorktree)
    assert "detached HEAD" in result.reason


def test_merged_pr_overrides_unmerged_git(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")
    wt.commit("novel", "unique\n", "landed via squash PR")
    result = _classify(repo, wt.path, proc, pr_states={"feature": PrInfo(42, PrState.MERGED)})
    assert isinstance(result, wg.PrunableWorktree)
    assert "PR #42 merged" in result.reason


def test_open_pr_is_kept(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")
    wt.commit("novel", "unique\n", "work in review")
    result = _classify(repo, wt.path, proc, pr_states={"feature": PrInfo(7, PrState.OPEN)})
    assert isinstance(result, wg.RetainedWorktree)
    assert result.reason == "open PR #7"


def test_live_process_is_kept(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")
    (proc / "1234").mkdir()
    (proc / "1234" / "cwd").symlink_to(wt.path)
    result = _classify(repo, wt.path, proc)
    assert isinstance(result, wg.RetainedWorktree)
    assert "process is working in it" in result.reason


def test_active_worktree_is_kept(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")
    result = _classify(repo, wt.path, proc, active_path=wt.path)
    assert isinstance(result, wg.RetainedWorktree)
    assert result.reason == "the invoking worktree"


def test_main_worktree_identified(repo: GitRepo) -> None:
    assert git_repo.main_worktree(repo.path) == repo.path


def test_remove_worktree_deletes_and_leaves_branch(repo: GitRepo) -> None:
    wt = repo.worktree("wt", "feature")

    result = wg.remove_worktree(repo.path, wt.path)

    assert result == wg.RemovedWorktree(wt.path)
    assert not wt.path.exists()
    assert repo.has_branch("feature")  # the work stays reachable through the branch


def test_remove_worktree_fails_on_dirty_tree(repo: GitRepo) -> None:
    wt = repo.worktree("wt", "feature")
    (wt.path / "base").write_text("dirtied after scan\n")

    result = wg.remove_worktree(repo.path, wt.path)

    # `git worktree remove` without --force refuses a dirty tree, so nothing is lost.
    assert isinstance(result, wg.FailedWorktree)
    assert wt.path.exists()


if __name__ == "__main__":
    pytest_bazel.main()
