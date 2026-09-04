import subprocess
from pathlib import Path

import pygit2
import pytest
import pytest_bazel

from devinfra.gc import branch_gc as bg
from devinfra.gc.git_repo import Worktree
from devinfra.gc.pull_request import PrInfo, PrState
from devinfra.gc.worktree_gc import PrunableWorktree, RetainedWorktree


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit(path: Path, name: str, content: str, message: str) -> None:
    (path / name).write_text(content)
    _git(path, "add", "--", name)
    _git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message)


def _rev(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "base", "0\n", "init")
    return repo


def _worktree(repo: Path, name: str, branch: str, start: str = "main") -> Path:
    path = repo.parent / name
    _git(repo, "worktree", "add", "-q", "-b", branch, str(path), start)
    return path


def _classify(
    repo: Path, name: str, *, pr: PrInfo | None = None, holder: bg.Holder = None, default_branch: str = "main"
) -> bg.BranchClassification:
    pg = pygit2.Repository(str(repo))
    return bg.classify_branch(name, pg=pg, main="main", default_branch=default_branch, pr=pr, holder=holder)


def test_ancestor_branch_is_prunable(repo: Path) -> None:
    _git(repo, "branch", "feature", "main")
    _commit(repo, "later", "1\n", "advance main")  # feature is now an ancestor of main
    result = _classify(repo, "feature")
    assert isinstance(result, bg.PrunableBranch)
    assert "already in main" in result.reason


def test_empty_branch_is_prunable(repo: Path) -> None:
    _git(repo, "branch", "feature", "main")
    assert isinstance(_classify(repo, "feature"), bg.PrunableBranch)


def test_squash_merged_branch_is_prunable(repo: Path) -> None:
    wt = _worktree(repo, "wt", "feature")
    _commit(wt, "shared", "same\n", "add on branch")
    _commit(repo, "shared", "same\n", "same change squashed onto main")
    # Merging feature into main is a no-op — git alone proves the content is already there.
    assert isinstance(_classify(repo, "feature"), bg.PrunableBranch)


def test_unique_branch_no_pr_is_review(repo: Path) -> None:
    wt = _worktree(repo, "wt", "feature")
    _commit(wt, "novel", "unique\n", "unmerged work")
    result = _classify(repo, "feature")
    assert isinstance(result, bg.ReviewBranch)
    assert "commits not in main" in result.reason


def test_merged_pr_with_content_in_main_is_prunable(repo: Path) -> None:
    _git(repo, "branch", "feature", "main")
    _commit(repo, "later", "1\n", "advance main")
    result = _classify(repo, "feature", pr=PrInfo(5, PrState.MERGED))
    assert isinstance(result, bg.PrunableBranch)
    assert "PR #5 merged" in result.reason


def test_squash_merged_pr_beyond_git_proof_is_prunable(repo: Path) -> None:
    # main squash-merged feature, then moved the same file on past it, so the git tree-merge
    # now conflicts — only the PR's merged head SHA proves nothing is lost.
    wt = _worktree(repo, "wt", "feature")
    _commit(wt, "f", "A\n", "feature change")
    head = _rev(repo, "feature")
    _commit(repo, "f", "A\n", "squash-merge onto main")
    _commit(repo, "f", "B\n", "main advances past the squash")
    result = _classify(repo, "feature", pr=PrInfo(7, PrState.MERGED, head_sha=head))
    assert isinstance(result, bg.PrunableBranch)
    assert "nothing beyond the merged head" in result.reason


def test_branch_advanced_past_merged_head_is_review(repo: Path) -> None:
    wt = _worktree(repo, "wt", "feature")
    _commit(wt, "f", "A\n", "feature change")
    head = _rev(repo, "feature")  # the merged tip
    _commit(wt, "extra", "more\n", "work past the merge")  # feature advances beyond it
    _commit(repo, "f", "A\n", "squash-merge onto main")
    _commit(repo, "f", "B\n", "main advances")
    result = _classify(repo, "feature", pr=PrInfo(7, PrState.MERGED, head_sha=head))
    assert isinstance(result, bg.ReviewBranch)
    assert "beyond it" in result.reason


def test_open_pr_is_kept(repo: Path) -> None:
    wt = _worktree(repo, "wt", "feature")
    _commit(wt, "novel", "unique\n", "work in review")
    result = _classify(repo, "feature", pr=PrInfo(9, PrState.OPEN))
    assert isinstance(result, bg.RetainedBranch)
    assert result.reason == "open PR #9"


def test_default_branch_is_kept(repo: Path) -> None:
    result = _classify(repo, "main")
    assert isinstance(result, bg.RetainedBranch)
    assert result.reason == "default branch"


def test_branch_in_retained_worktree_is_kept(repo: Path) -> None:
    _git(repo, "branch", "feature", "main")
    holder = RetainedWorktree(Worktree(path=Path("/wt"), branch="feature"), "uncommitted changes", None)
    result = _classify(repo, "feature", holder=holder)
    assert isinstance(result, bg.RetainedBranch)
    assert "retained worktree /wt" in result.reason


def test_branch_in_prunable_worktree_is_prunable(repo: Path) -> None:
    _git(repo, "branch", "feature", "main")
    _commit(repo, "later", "1\n", "advance main")
    holder = PrunableWorktree(Worktree(path=Path("/wt"), branch="feature"), "changes already in main", None)
    result = _classify(repo, "feature", holder=holder)
    assert isinstance(result, bg.PrunableBranch)
    assert result.checkout == Path("/wt")


def test_delete_branch_removes_it(repo: Path) -> None:
    _git(repo, "branch", "feature", "main")
    assert bg.delete_branch(repo, "feature") == bg.RemovedBranch("feature")
    listing = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "feature"], check=True, capture_output=True, text=True
    ).stdout
    assert "feature" not in listing


def test_delete_branch_fails_when_checked_out(repo: Path) -> None:
    _worktree(repo, "wt", "feature")
    assert isinstance(bg.delete_branch(repo, "feature"), bg.FailedBranch)


def test_cherry_picked_branch_past_the_tree_check_is_prunable(repo: Path) -> None:
    """A branch whose commit landed by cherry-pick, on a main that has since moved past it.

    This is the shape the tree-equality check decays on: main took the same patch and then
    kept editing the same file, so merging the branch in today conflicts and
    `content_in_main` reports it unlanded. Patch equivalence still recognises it, with no PR
    to appeal to.
    """
    wt = _worktree(repo, "wt", "feature")
    _commit(wt, "f", "A\n", "feature change")
    _git(repo, "cherry-pick", _rev(repo, "feature"))
    _commit(repo, "f", "B\n", "main advances past the cherry-pick")
    _commit(repo, "f", "C\n", "and again")

    result = _classify(repo, "feature", pr=None)

    assert isinstance(result, bg.PrunableBranch)
    assert "equivalent already on" in result.reason


def test_branch_with_an_unlanded_commit_stays_review(repo: Path) -> None:
    """One equivalent commit is not enough — an unlanded sibling must still hold it back."""
    wt = _worktree(repo, "wt", "feature")
    _commit(wt, "f", "A\n", "landed change")
    _git(repo, "cherry-pick", _rev(repo, "feature"))
    _commit(wt, "g", "never\n", "change main never took")

    result = _classify(repo, "feature", pr=None)

    assert isinstance(result, bg.ReviewBranch)


if __name__ == "__main__":
    pytest_bazel.main()
