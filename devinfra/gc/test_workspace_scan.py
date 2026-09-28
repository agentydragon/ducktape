import threading
from pathlib import Path

import pytest_bazel

from devinfra.gc import output_base_gc, workspace_scan
from devinfra.gc.branch_gc import PrunableBranch, RetainedBranch
from devinfra.gc.conftest import GitRepo, make_base
from devinfra.gc.output_base_gc import Inspection, PrunableBase, RetainedBase, ReviewBase
from devinfra.gc.worktree_gc import PrunableWorktree, RetainedWorktree


def _add(repo: GitRepo, name: str, branch: str) -> Path:
    return repo.worktree(name, branch).path


def _scan(
    repo: GitRepo, proc: Path, mountinfo: Path, output_user_root: Path | None = None
) -> workspace_scan.WorkspaceScan:
    return workspace_scan.scan_workspace(
        repo.path,
        main="main",
        default_branch="main",
        pr_states={},
        output_user_root=output_user_root,
        proc_root=proc,
        mountinfo_path=mountinfo,
    )


class RecordingProgress:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int, str]] = []
        self.totals: dict[str, int] = {}
        self.done: dict[str, int] = {}
        self.lock = threading.Lock()

    def start_phase(self, phase: str, total: int) -> None:
        with self.lock:
            self.totals[phase] = total
            self.done[phase] = 0

    def record(self, phase: str, category: str) -> None:
        with self.lock:
            self.done[phase] += 1
            self.calls.append((phase, self.done[phase], self.totals[phase], category))


def test_branch_follows_its_holding_worktree(repo: GitRepo, proc: Path, mountinfo: Path) -> None:
    prunable_wt = _add(repo, "wt_merged", "merged")  # empty branch → prunable worktree
    kept_wt = _add(repo, "wt_kept", "kept")
    (kept_wt / "scratch").write_text("x\n")  # dirty → retained worktree

    scan = _scan(repo, proc, mountinfo)

    worktrees = {item.worktree.path: item for item in scan.worktrees}
    assert isinstance(worktrees[prunable_wt], PrunableWorktree)
    assert isinstance(worktrees[kept_wt], RetainedWorktree)

    branches = {item.branch.name: item for item in scan.branches}
    # Held by a prunable worktree → eligible, with the worktree recorded for apply ordering.
    assert isinstance(branches["merged"], PrunableBranch)
    assert branches["merged"].checkout == prunable_wt
    # Held by a retained worktree → not provably deletable.
    assert isinstance(branches["kept"], RetainedBranch)
    assert "retained worktree" in branches["kept"].reason
    # The default branch, checked out on the main worktree, is always kept.
    assert isinstance(branches["main"], RetainedBranch)
    assert branches["main"].reason == "default branch"


def test_scan_workspace_reports_progress(repo: GitRepo, proc: Path, mountinfo: Path) -> None:
    """The progress sink drives the CLI's live indicator (workspace_gc._ProgressReporter).

    Branches classify from a thread pool, so this pins the guarantee that matters for a
    concurrent reporter: every completion still reports a distinct `done` count that reaches
    `total`, whichever worker reports it.
    """
    _add(repo, "wt_a", "branch_a")
    _add(repo, "wt_b", "branch_b")

    progress = RecordingProgress()

    workspace_scan.scan_workspace(
        repo.path,
        main="main",
        default_branch="main",
        pr_states={},
        proc_root=proc,
        mountinfo_path=mountinfo,
        progress=progress,
    )

    worktree_calls = [call[1:] for call in progress.calls if call[0] == "worktrees"]
    branch_calls = [call[1:] for call in progress.calls if call[0] == "branches"]
    assert worktree_calls[-1][:2] == (2, 2)  # wt_a, wt_b
    assert branch_calls[-1][:2] == (3, 3)  # main, branch_a, branch_b
    assert sorted(done for done, _, _ in branch_calls) == [1, 2, 3]
    assert sum(call[3] == "PRUNE" for call in progress.calls if call[0] == "worktrees") == 2
    assert sum(call[3] == "PRUNE" for call in progress.calls if call[0] == "branches") == 2
    assert sum(call[3] == "KEEP" for call in progress.calls if call[0] == "branches") == 1


def test_base_whose_workspace_is_a_prunable_worktree_is_annotated(
    repo: GitRepo, proc: Path, mountinfo: Path, tmp_path: Path
) -> None:
    prunable_wt = _add(repo, "wt_merged", "merged")  # a prunable worktree, still on disk
    root = tmp_path / "output"
    make_base(root, prunable_wt.resolve())  # its workspace is that worktree

    scan = _scan(repo, proc, mountinfo, output_user_root=root)

    (base,) = scan.bases
    # The workspace exists (the worktree is still there), so the base is retained for now …
    assert isinstance(base, RetainedBase)
    # … but flagged because pruning the worktree would orphan it.
    assert "prunable worktree" in base.reason


def test_base_with_absent_workspace_is_prunable(repo: GitRepo, proc: Path, mountinfo: Path, tmp_path: Path) -> None:
    root = tmp_path / "output"
    make_base(root, tmp_path / "gone")

    scan = _scan(repo, proc, mountinfo, output_user_root=root)

    assert [type(base) for base in scan.bases] == [PrunableBase]


def _verdict(base: Inspection) -> tuple[type, Path, str | None]:
    """Everything a base's row is rendered from, so the comparison is not just the class."""
    reason = base.reason if isinstance(base, RetainedBase | ReviewBase) else None
    return (type(base), base.path, reason)


def _annotated(repo: GitRepo, proc: Path, mountinfo: Path, root: Path) -> list[Inspection]:
    bases = list(output_base_gc.scan_output_user_root(root, proc_root=proc, mountinfo_path=mountinfo))
    return workspace_scan.annotate_bases(repo.path, bases, main="main", pr_states={}, proc_root=proc)


def test_annotate_bases_matches_the_joint_scan(repo: GitRepo, proc: Path, mountinfo: Path, tmp_path: Path) -> None:
    """The bases-only path must reach the same verdicts as `scan_workspace`.

    It skips branch classification and every worktree that is not a base workspace, so this
    pins that the shortcut does not change an answer — including the annotation, which is the
    one thing the shortcut still has to look at worktrees for.
    """
    prunable_wt = _add(repo, "wt_merged", "merged")
    _add(repo, "wt_unrelated", "unrelated")  # never a base workspace; the fast path skips it
    root = tmp_path / "output"
    make_base(root, prunable_wt.resolve())
    make_base(root, (repo.path.parent / "gone").resolve())  # workspace absent → prunable base

    joint = _scan(repo, proc, mountinfo, output_user_root=root).bases
    fast = _annotated(repo, proc, mountinfo, root)

    assert [_verdict(item) for item in fast] == [_verdict(item) for item in joint]
    assert any(isinstance(item, PrunableBase) for item in fast)
    assert any(isinstance(item, RetainedBase) and "prunable worktree" in item.reason for item in fast)


def test_annotate_bases_needs_no_worktree_scan_when_no_base_has_a_workspace(
    repo: GitRepo, proc: Path, mountinfo: Path, tmp_path: Path
) -> None:
    """With no retained base pointing at a live worktree there is nothing to annotate."""
    _add(repo, "wt_merged", "merged")
    root = tmp_path / "output"
    make_base(root, (repo.path.parent / "gone").resolve())

    (base,) = _annotated(repo, proc, mountinfo, root)

    assert isinstance(base, PrunableBase)


def test_base_workspace_branches_covers_only_base_workspaces(
    repo: GitRepo, proc: Path, mountinfo: Path, tmp_path: Path
) -> None:
    """The PR query is scoped to base workspaces, not every branch in the repo."""
    workspace_wt = _add(repo, "wt_workspace", "has_base")
    _add(repo, "wt_unrelated", "no_base")
    root = tmp_path / "output"
    make_base(root, workspace_wt.resolve())

    bases = list(output_base_gc.scan_output_user_root(root, proc_root=proc, mountinfo_path=mountinfo))

    assert workspace_scan.base_workspace_branches(repo.path, bases) == {"has_base"}


if __name__ == "__main__":
    pytest_bazel.main()
