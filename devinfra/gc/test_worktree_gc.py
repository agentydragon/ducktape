import os
import shutil
import subprocess
import time
from enum import StrEnum
from pathlib import Path
from typing import cast

import pytest
import pytest_bazel

from devinfra.gc import git_repo, worktree_gc as wg
from devinfra.gc.conftest import GitRepo
from devinfra.gc.git_repo import Worktree
from devinfra.gc.pull_request import PrInfo, PrState
from devinfra.gc.removal import Failed, Removed


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
    assert _classify(repo, wt.path, proc).reason is wg.PrunableWorktreeReason.CONTENT_IN_MAIN


def test_squash_merge_is_prunable(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")
    wt.commit("shared", "same\n", "add on branch")
    repo.commit("shared", "same\n", "same change squashed onto main")
    # Merging the branch into main is now a no-op — its content is already there.
    assert _classify(repo, wt.path, proc).reason is wg.PrunableWorktreeReason.CONTENT_IN_MAIN


def test_empty_branch_is_prunable(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")  # no commits beyond main
    result = _classify(repo, wt.path, proc)
    assert isinstance(result, wg.PrunableWorktree)
    assert (result.reason, result.main) == (wg.PrunableWorktreeReason.CONTENT_IN_MAIN, "main")


def test_unique_unmerged_is_review(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")
    wt.commit("novel", "unique\n", "unmerged work")
    result = _classify(repo, wt.path, proc)
    assert isinstance(result, wg.ReviewWorktree)
    assert (result.reason, result.main) == (wg.ReviewWorktreeReason.UNMERGED, "main")


def test_dirty_tracked_change_is_kept(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")
    (wt.path / "base").write_text("dirty\n")
    assert _classify(repo, wt.path, proc).reason is wg.RetainedWorktreeReason.UNCOMMITTED_CHANGES


def test_dirty_tracked_deletion_is_kept(repo: GitRepo, proc: Path) -> None:
    """A tracked file deleted out from under the worktree (not just modified) must still be
    caught by the tracked-only fast path, not just a tracked modification."""
    wt = repo.worktree("wt", "feature")
    (wt.path / "base").unlink()
    assert _classify(repo, wt.path, proc).reason is wg.RetainedWorktreeReason.UNCOMMITTED_CHANGES


def _record_status_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch `worktree_gc.git` to record each `git status` call's `--untracked-files` mode
    while still delegating to the real implementation."""
    calls: list[str] = []
    real_git = wg.git

    def recording_git(repo: Path, *args: str, **kwargs: bool) -> subprocess.CompletedProcess[str]:
        calls.extend(arg.removeprefix("--untracked-files=") for arg in args if arg.startswith("--untracked-files="))
        return real_git(repo, *args, **kwargs)

    monkeypatch.setattr(wg, "git", recording_git)
    return calls


def test_tracked_only_dirty_check_skips_the_untracked_walk(
    repo: GitRepo, proc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worktree already dirty via a tracked change must never pay for the separate,
    much more expensive full status call that also walks for untracked files."""
    wt = repo.worktree("wt", "feature")
    (wt.path / "base").write_text("dirty\n")
    calls = _record_status_calls(monkeypatch)

    _classify(repo, wt.path, proc)

    assert calls == ["no"]


def test_clean_on_tracked_files_falls_back_to_the_full_status(
    repo: GitRepo, proc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worktree clean on tracked files must still fall back to the full scan, so an
    untracked file is not missed."""
    wt = repo.worktree("wt", "feature")
    (wt.path / "scratch").write_text("x\n")
    calls = _record_status_calls(monkeypatch)

    result = _classify(repo, wt.path, proc)

    assert calls == ["no", "normal"]
    assert result.reason is wg.RetainedWorktreeReason.UNCOMMITTED_CHANGES


@pytest.mark.parametrize(
    "pr", [pytest.param(PrInfo(9, PrState.OPEN), id="open"), pytest.param(PrInfo(5, PrState.MERGED), id="merged")]
)
def test_dirty_with_pr_is_kept_and_flagged(repo: GitRepo, proc: Path, pr: PrInfo) -> None:
    # Uncommitted work always wins over a PR-based verdict, but the classification carries the PR
    # so a dirty tree whose PR already merged reads as stale scratch worth clearing by hand.
    wt = repo.worktree("wt", "feature")
    (wt.path / "base").write_text("dirty\n")
    without_pr = _classify(repo, wt.path, proc)
    result = _classify(repo, wt.path, proc, pr_states={"feature": pr})
    assert isinstance(without_pr, wg.RetainedWorktree)
    assert isinstance(result, wg.RetainedWorktree)
    assert (without_pr.reason, without_pr.pr) == (wg.RetainedWorktreeReason.UNCOMMITTED_CHANGES, None)
    assert (result.reason, result.pr) == (wg.RetainedWorktreeReason.UNCOMMITTED_CHANGES, pr)


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
    assert (result.reason, result.main) == (wg.PrunableWorktreeReason.DIRECTORY_MISSING, "main")
    assert result.last_activity is None


def test_existing_worktree_with_broken_gitdir_is_review(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")
    (wt.path / ".git").write_text("gitdir: /nonexistent/worktree-admin\n")

    result = _classify(repo, wt.path, proc)

    assert isinstance(result, wg.ReviewWorktree)
    assert result.reason is wg.ReviewWorktreeReason.INSPECTION_FAILED
    assert "cannot inspect" in wg.describe_reason(result)
    assert wt.path.is_dir()


@pytest.mark.parametrize("stage", ["_status", "_last_activity", "content_in_main", "patches_landed_in_main"])
@pytest.mark.parametrize("error_type", [KeyError, ValueError, PermissionError])
def test_worktree_inspection_failure_is_review(
    repo: GitRepo, proc: Path, monkeypatch: pytest.MonkeyPatch, stage: str, error_type: type[Exception]
) -> None:
    wt = repo.worktree("wt", "feature")
    wt.commit("novel", "unique\n", "unmerged work")

    def fail(*args: object, **kwargs: object) -> None:
        raise error_type("unreadable Git state")

    monkeypatch.setattr(wg, stage, fail)

    result = _classify(repo, wt.path, proc)

    assert isinstance(result, wg.ReviewWorktree)
    assert result.reason is wg.ReviewWorktreeReason.INSPECTION_FAILED
    assert "unreadable Git state" in wg.describe_reason(result)


def test_detached_head_with_commit_is_review(repo: GitRepo, proc: Path) -> None:
    path = repo.path.parent / "wt"
    repo.run("worktree", "add", "-q", "--detach", str(path), "main")
    GitRepo(path).commit("novel", "unique\n", "detached work")
    assert _classify(repo, path, proc).reason is wg.ReviewWorktreeReason.DETACHED_HEAD


@pytest.mark.parametrize(
    ("pr", "expected_type", "expected_reason"),
    [
        pytest.param(PrInfo(42, PrState.MERGED), wg.PrunableWorktree, wg.PrunableWorktreeReason.PR_LANDED, id="merged"),
        # A closed unmerged PR leaves its branch ref reachable; pruning the tree loses no commits.
        pytest.param(PrInfo(11, PrState.CLOSED), wg.PrunableWorktree, wg.PrunableWorktreeReason.PR_LANDED, id="closed"),
        pytest.param(PrInfo(7, PrState.OPEN), wg.RetainedWorktree, wg.RetainedWorktreeReason.OPEN_PR, id="open"),
    ],
)
def test_pr_state_classifies_unmerged_worktree(
    repo: GitRepo,
    proc: Path,
    pr: PrInfo,
    expected_type: type[wg.PrunableWorktree] | type[wg.RetainedWorktree],
    expected_reason: wg.PrunableWorktreeReason | wg.RetainedWorktreeReason,
) -> None:
    wt = repo.worktree("wt", "feature")
    wt.commit("novel", "unique\n", "unmerged work")
    result = _classify(repo, wt.path, proc, pr_states={"feature": pr})
    assert isinstance(result, expected_type)
    assert (result.reason, cast(wg.PrunableWorktree | wg.RetainedWorktree, result).pr) == (expected_reason, pr)


def test_live_process_is_kept(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")
    (proc / "1234").mkdir()
    (proc / "1234" / "cwd").symlink_to(wt.path)
    result = _classify(repo, wt.path, proc)
    assert isinstance(result, wg.RetainedWorktree)
    assert (result.reason, result.pid) == (wg.RetainedWorktreeReason.LIVE_PROCESS, 1234)


def test_active_worktree_is_kept(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")
    result = _classify(repo, wt.path, proc, active_path=wt.path)
    assert result.reason is wg.RetainedWorktreeReason.INVOKING_WORKTREE


def test_main_worktree_identified(repo: GitRepo) -> None:
    assert git_repo.main_worktree(repo.path) == repo.path


def test_remove_worktree_deletes_and_leaves_branch(repo: GitRepo) -> None:
    wt = repo.worktree("wt", "feature")

    result = wg.remove_worktree(repo.path, wt.path)

    assert result == Removed(wt.path)
    assert not wt.path.exists()
    assert repo.has_branch("feature")  # the work stays reachable through the branch


def test_remove_worktree_fails_on_dirty_tree(repo: GitRepo) -> None:
    wt = repo.worktree("wt", "feature")
    (wt.path / "base").write_text("dirtied after scan\n")

    result = wg.remove_worktree(repo.path, wt.path)

    # `git worktree remove` without --force refuses a dirty tree, so nothing is lost.
    assert isinstance(result, Failed)
    assert wt.path.exists()


_MAIN = "origin/test-main"
_PR = PrInfo(7421, PrState.MERGED)


def _populated(reason: StrEnum) -> wg.Classification:
    """The classification `reason` belongs to, carrying all the evidence a reason can cite."""
    worktree = Worktree(path=Path("/wt"), branch="feature")
    match reason:
        case wg.PrunableWorktreeReason():
            return wg.PrunableWorktree(worktree, reason, None, _MAIN, _PR)
        case wg.RetainedWorktreeReason():
            return wg.RetainedWorktree(worktree, reason, None, _PR, 4242)
        case wg.ReviewWorktreeReason():
            return wg.ReviewWorktree(worktree, reason, None, _MAIN, detail="test error")
    raise TypeError(reason)


@pytest.mark.parametrize(
    "reason", [*wg.PrunableWorktreeReason, *wg.RetainedWorktreeReason, *wg.ReviewWorktreeReason], ids=str
)
def test_every_reason_is_described(reason: StrEnum) -> None:
    assert wg.describe_reason(_populated(reason))


@pytest.mark.parametrize(
    ("reason", "evidence"),
    [
        pytest.param(wg.PrunableWorktreeReason.PR_LANDED, str(_PR.number), id="landed-pr"),
        pytest.param(wg.RetainedWorktreeReason.OPEN_PR, str(_PR.number), id="open-pr"),
        pytest.param(wg.RetainedWorktreeReason.UNCOMMITTED_CHANGES, str(_PR.number), id="dirty-with-pr"),
        pytest.param(wg.RetainedWorktreeReason.LIVE_PROCESS, "4242", id="pid"),
        pytest.param(wg.PrunableWorktreeReason.CONTENT_IN_MAIN, _MAIN, id="content-main"),
        pytest.param(wg.PrunableWorktreeReason.PATCHES_IN_MAIN, _MAIN, id="patches-main"),
        pytest.param(wg.ReviewWorktreeReason.DETACHED_HEAD, _MAIN, id="detached-main"),
        pytest.param(wg.ReviewWorktreeReason.UNMERGED, _MAIN, id="unmerged-main"),
    ],
)
def test_the_description_names_what_the_reason_cites(reason: StrEnum, evidence: str) -> None:
    assert evidence in wg.describe_reason(_populated(reason))


if __name__ == "__main__":
    pytest_bazel.main()
