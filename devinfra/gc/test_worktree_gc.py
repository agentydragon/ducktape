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
from devinfra.gc.removal import Failed, RemovalResult, Removed, Skipped


def _classify(repo: GitRepo, path: Path, proc: Path, **kwargs: object) -> wg.Classification:
    kwargs.setdefault("pr_states", {})
    kwargs.setdefault("active_path", None)
    main_path = git_repo.main_worktree(repo.path)
    linked = [wt for wt in git_repo.list_worktrees(repo.path) if wt.path != main_path]
    live = wg.processes_by_worktree((wt.path for wt in linked), proc_root=proc)
    worktree = next(wt for wt in linked if wt.path == path)
    return wg.classify_worktree(worktree, main="main", main_path=main_path, live_pids=live.get(path, []), **kwargs)  # type: ignore[arg-type]


def _ignore_in_repo(repo: GitRepo, *patterns: str) -> None:
    exclude = repo.path / ".git" / "info" / "exclude"
    exclude.write_text("\n".join(patterns) + "\n")


def _commit_detached(wt: GitRepo, message: str) -> None:
    """Create an empty commit through Git so it updates this linked worktree's detached HEAD."""
    wt.run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", message)


def _surviving_ref_commits(repo: GitRepo) -> list[str]:
    """Commits reachable from named refs; unlike `--all`, omit linked-worktree HEADs."""
    return repo.run("rev-list", "--branches", "--tags", "--remotes").stdout.splitlines()


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


def test_detached_landed_commit_is_removed_and_preserved(repo: GitRepo, proc: Path) -> None:
    path = repo.path.parent / "wt"
    repo.run("worktree", "add", "-q", "--detach", str(path), "main")
    detached = GitRepo(path)
    _commit_detached(detached, "detached empty commit")
    head = detached.rev("HEAD")
    result = _classify(repo, path, proc)
    assert isinstance(result, wg.PrunableWorktree)
    removal = wg.remove_if_still_prunable(
        repo.path, Worktree(path, None), main="main", pr_states={}, main_path=repo.path, active_path=None, live_pids=[]
    )
    assert removal == Removed(path)
    assert repo.rev(f"refs/workspace-gc/recovery/{head}") == head


def test_detached_reflog_commits_survive_removal(repo: GitRepo) -> None:
    path = repo.path.parent / "wt"
    repo.run("worktree", "add", "-q", "--detach", str(path), "main")
    detached = GitRepo(path)
    _commit_detached(detached, "abandoned commit")
    abandoned = detached.rev("HEAD")
    detached.run("reset", "--hard", "main")
    result = wg.remove_if_still_prunable(
        repo.path, Worktree(path, None), main="main", pr_states={}, main_path=repo.path, active_path=None, live_pids=[]
    )
    assert result == Removed(path)
    assert repo.rev(f"refs/workspace-gc/recovery/{abandoned}") == abandoned


def test_detached_reflog_oldest_old_oid_survives(repo: GitRepo) -> None:
    path = repo.path.parent / "wt"
    repo.run("worktree", "add", "-q", "--detach", str(path), "main")
    detached = GitRepo(path)
    _commit_detached(detached, "abandoned commit")
    abandoned = detached.rev("HEAD")
    detached.run("reset", "--hard", "main")
    reflog = Path(detached.run("rev-parse", "--path-format=absolute", "--git-path", "logs/HEAD").stdout.strip())
    last_entry = reflog.read_text().splitlines()[-1]
    assert last_entry.split()[0] == abandoned
    reflog.write_text(last_entry + "\n")
    assert abandoned not in detached.run("reflog", "show", "--format=%H", "HEAD").stdout
    result = wg.remove_if_still_prunable(
        repo.path, Worktree(path, None), main="main", pr_states={}, main_path=repo.path, active_path=None, live_pids=[]
    )
    assert result == Removed(path)
    assert repo.rev(f"refs/workspace-gc/recovery/{abandoned}") == abandoned


def test_detached_patch_landed_even_when_main_changed_again(repo: GitRepo, proc: Path) -> None:
    path = repo.path.parent / "wt"
    repo.run("worktree", "add", "-q", "--detach", str(path), "main")
    detached = GitRepo(path)
    detached.commit("base", "1\n", "first change")
    head = detached.rev("HEAD")
    repo.commit("base", "1\n", "same patch")
    repo.commit("base", "2\n", "subsequent change")
    result = _classify(repo, path, proc)
    assert result.reason is wg.PrunableWorktreeReason.PATCHES_IN_MAIN
    assert wg.remove_if_still_prunable(
        repo.path, Worktree(path, None), main="main", pr_states={}, main_path=repo.path, active_path=None, live_pids=[]
    ) == Removed(path)
    assert repo.rev(f"refs/workspace-gc/recovery/{head}") == head


def test_detached_head_change_during_preservation_stops_removal(repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    path = repo.path.parent / "wt"
    repo.run("worktree", "add", "-q", "--detach", str(path), "main")
    detached = GitRepo(path)
    real_git = wg.git
    changed = False

    def change_head(repo_path: Path, *args: str, **kwargs: bool) -> subprocess.CompletedProcess[str]:
        nonlocal changed
        result = real_git(repo_path, *args, **kwargs)
        if args[0] == "update-ref" and not changed:
            changed = True
            _commit_detached(detached, "concurrent commit")
        return result

    monkeypatch.setattr(wg, "git", change_head)
    result = wg.remove_if_still_prunable(
        repo.path, Worktree(path, None), main="main", pr_states={}, main_path=repo.path, active_path=None, live_pids=[]
    )
    assert isinstance(result, Skipped)
    assert path.exists()


def test_detached_unmerged_commit_is_review(repo: GitRepo, proc: Path) -> None:
    path = repo.path.parent / "wt"
    repo.run("worktree", "add", "-q", "--detach", str(path), "main")
    detached = GitRepo(path)
    detached.commit("novel", "unique\n", "unmerged work")
    assert isinstance(_classify(repo, path, proc), wg.ReviewWorktree)


def test_detached_worktree_is_not_directly_removable(repo: GitRepo) -> None:
    path = repo.path.parent / "wt"
    repo.run("worktree", "add", "-q", "--detach", str(path), "main")
    detached = GitRepo(path)
    _commit_detached(detached, "detached empty commit")
    detached_head = detached.rev("HEAD")
    assert detached_head not in _surviving_ref_commits(repo)

    result = wg.remove_worktree(repo.path, path)

    assert isinstance(result, Skipped)
    assert GitRepo(path).rev("HEAD") == detached_head
    assert path.exists()


def test_missing_detached_worktree_is_review(repo: GitRepo, proc: Path) -> None:
    path = repo.path.parent / "wt"
    repo.run("worktree", "add", "-q", "--detach", str(path), "main")
    _commit_detached(GitRepo(path), "detached empty commit")
    shutil.rmtree(path)

    result = _classify(repo, path, proc)

    assert isinstance(result, wg.ReviewWorktree)
    assert result.reason is wg.ReviewWorktreeReason.DETACHED_HEAD


def test_ignored_files_do_not_block_removal(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")
    _ignore_in_repo(repo, ".env")
    (wt.path / ".env").write_text("local data\n")
    assert isinstance(_classify(repo, wt.path, proc), wg.PrunableWorktree)
    assert _remove_if_still_prunable(repo, wt, live_pids=[]) == Removed(wt.path)
    assert not wt.path.exists()


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


def test_remove_worktree_skips_missing_directory_for_git_worktree_prune(repo: GitRepo) -> None:
    wt = repo.worktree("wt", "feature")
    shutil.rmtree(wt.path)

    result = wg.remove_worktree(repo.path, wt.path)

    assert isinstance(result, Skipped)
    assert "prune its stale Git metadata separately" in result.reason
    assert any(item.path == wt.path for item in git_repo.list_worktrees(repo.path))


def _remove_if_still_prunable(repo: GitRepo, wt: GitRepo, *, live_pids: list[int]) -> RemovalResult[Path]:
    return wg.remove_if_still_prunable(
        repo.path,
        Worktree(path=wt.path, branch="feature"),
        main="main",
        pr_states={},
        main_path=repo.path,
        active_path=None,
        live_pids=live_pids,
    )


def test_remove_if_still_prunable_removes_an_unchanged_tree(repo: GitRepo) -> None:
    wt = repo.worktree("wt", "feature")

    assert _remove_if_still_prunable(repo, wt, live_pids=[]) == Removed(wt.path)
    assert not wt.path.exists()


def test_remove_if_still_prunable_skips_when_worktree_detaches_after_scan(repo: GitRepo, proc: Path) -> None:
    wt = repo.worktree("wt", "feature")
    assert isinstance(_classify(repo, wt.path, proc), wg.PrunableWorktree)
    wt.run("switch", "--detach")
    _commit_detached(wt, "detached empty commit")
    detached_head = wt.rev("HEAD")
    assert detached_head not in _surviving_ref_commits(repo)

    result = _remove_if_still_prunable(repo, wt, live_pids=[])

    assert isinstance(result, Skipped)
    assert wt.path.exists()
    assert wt.rev("HEAD") == detached_head


def test_remove_if_still_prunable_skips_when_branch_changes_after_scan(repo: GitRepo) -> None:
    wt = repo.worktree("wt", "feature")
    worktree = Worktree(wt.path, "feature")
    pr_states = {"feature": PrInfo(13, PrState.MERGED)}
    assert isinstance(
        wg.classify_worktree(
            worktree, main="main", pr_states=pr_states, main_path=repo.path, active_path=None, live_pids=[]
        ),
        wg.PrunableWorktree,
    )
    wt.run("switch", "-c", "new-open-pr")

    result = wg.remove_if_still_prunable(
        repo.path, worktree, main="main", pr_states=pr_states, main_path=repo.path, active_path=None, live_pids=[]
    )

    assert isinstance(result, Skipped)
    assert "branch changed" in result.reason
    assert wt.path.exists()


def test_remove_if_still_prunable_skips_a_tree_a_process_entered_since_the_scan(repo: GitRepo) -> None:
    wt = repo.worktree("wt", "feature")

    result = _remove_if_still_prunable(repo, wt, live_pids=[4242])

    assert isinstance(result, Skipped)
    assert wt.path.exists()


def test_remove_if_still_prunable_skips_a_tree_with_uncommitted_work_since_the_scan(repo: GitRepo) -> None:
    wt = repo.worktree("wt", "feature")
    (wt.path / "notes").write_text("new since the scan\n")

    result = _remove_if_still_prunable(repo, wt, live_pids=[])

    assert isinstance(result, Skipped)
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
        pytest.param(wg.ReviewWorktreeReason.DETACHED_HEAD, "reachability", id="detached-head"),
        pytest.param(wg.ReviewWorktreeReason.UNMERGED, _MAIN, id="unmerged-main"),
    ],
)
def test_the_description_names_what_the_reason_cites(reason: StrEnum, evidence: str) -> None:
    assert evidence in wg.describe_reason(_populated(reason))


if __name__ == "__main__":
    pytest_bazel.main()
