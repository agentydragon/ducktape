from pathlib import Path

import pygit2
import pytest
import pytest_bazel

from devinfra.gc import branch_gc as bg
from devinfra.gc.conftest import GitRepo
from devinfra.gc.git_repo import Worktree
from devinfra.gc.pull_request import PrInfo, PrState
from devinfra.gc.worktree_gc import PrunableWorktree, RetainedWorktree


def _classify(
    repo: GitRepo, name: str, *, pr: PrInfo | None = None, holder: bg.Holder = None, default_branch: str = "main"
) -> bg.BranchClassification:
    pg = pygit2.Repository(str(repo.path))
    return bg.classify_branch(name, pg=pg, main="main", default_branch=default_branch, pr=pr, holder=holder)


def test_ancestor_branch_is_prunable(repo: GitRepo) -> None:
    repo.branch("feature")
    repo.commit("later", "1\n", "advance main")  # feature is now an ancestor of main
    result = _classify(repo, "feature")
    assert isinstance(result, bg.PrunableBranch)
    assert "already in main" in result.reason


def test_empty_branch_is_prunable(repo: GitRepo) -> None:
    repo.branch("feature")
    assert isinstance(_classify(repo, "feature"), bg.PrunableBranch)


def test_squash_merged_branch_is_prunable(repo: GitRepo) -> None:
    wt = repo.worktree("wt", "feature")
    wt.commit("shared", "same\n", "add on branch")
    repo.commit("shared", "same\n", "same change squashed onto main")
    # Merging feature into main is a no-op — git alone proves the content is already there.
    assert isinstance(_classify(repo, "feature"), bg.PrunableBranch)


def test_squash_merged_branch_survives_missing_blob(repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    # A partial clone (`blob:none`) may not have fetched every blob the pygit2 three-way merge
    # in content_in_main touches, and libgit2 has no lazy-fetch fallback for that (unlike the
    # `git` CLI `patches_landed_in_main` shells out to). Simulate the missing-object error and
    # check classification still lands on the git-CLI fallback instead of crashing.
    wt = repo.worktree("wt", "feature")
    wt.commit("shared", "same\n", "add on branch")
    repo.commit("shared", "same\n", "same change squashed onto main")

    def _raise_missing_object(*_args: object, **_kwargs: object) -> pygit2.Index:
        raise KeyError("object not found - no match for id deadbeef")

    monkeypatch.setattr(pygit2.Repository, "merge_commits", _raise_missing_object)
    result = _classify(repo, "feature")
    assert isinstance(result, bg.PrunableBranch)
    assert "equivalent already on main" in result.reason


def test_unique_branch_no_pr_is_review(repo: GitRepo) -> None:
    wt = repo.worktree("wt", "feature")
    wt.commit("novel", "unique\n", "unmerged work")
    result = _classify(repo, "feature")
    assert isinstance(result, bg.ReviewBranch)
    assert "commits not in main" in result.reason


def test_merged_pr_with_content_in_main_is_prunable(repo: GitRepo) -> None:
    repo.branch("feature")
    repo.commit("later", "1\n", "advance main")
    result = _classify(repo, "feature", pr=PrInfo(5, PrState.MERGED))
    assert isinstance(result, bg.PrunableBranch)
    assert "PR #5 merged" in result.reason


def test_squash_merged_pr_beyond_git_proof_is_prunable(repo: GitRepo) -> None:
    # main squash-merged feature, then moved the same file on past it, so the git tree-merge
    # now conflicts — only the PR's merged head SHA proves nothing is lost.
    wt = repo.worktree("wt", "feature")
    wt.commit("f", "A\n", "feature change")
    head = repo.rev("feature")
    repo.commit("f", "A\n", "squash-merge onto main")
    repo.commit("f", "B\n", "main advances past the squash")
    result = _classify(repo, "feature", pr=PrInfo(7, PrState.MERGED, head_sha=head))
    assert isinstance(result, bg.PrunableBranch)
    assert "nothing beyond the merged head" in result.reason


def test_branch_advanced_past_merged_head_is_review(repo: GitRepo) -> None:
    wt = repo.worktree("wt", "feature")
    wt.commit("f", "A\n", "feature change")
    head = repo.rev("feature")  # the merged tip
    wt.commit("extra", "more\n", "work past the merge")  # feature advances beyond it
    repo.commit("f", "A\n", "squash-merge onto main")
    repo.commit("f", "B\n", "main advances")
    result = _classify(repo, "feature", pr=PrInfo(7, PrState.MERGED, head_sha=head))
    assert isinstance(result, bg.ReviewBranch)
    assert "beyond it" in result.reason


def test_closed_pr_with_nothing_beyond_its_head_is_prunable(repo: GitRepo) -> None:
    # The PR was closed unmerged; nothing was committed on the branch since. Removing it
    # loses nothing a human hasn't already decided not to pursue.
    wt = repo.worktree("wt", "feature")
    wt.commit("f", "A\n", "abandoned attempt")
    head = repo.rev("feature")
    result = _classify(repo, "feature", pr=PrInfo(11, PrState.CLOSED, head_sha=head))
    assert isinstance(result, bg.PrunableBranch)
    assert "nothing beyond the closed head" in result.reason


def test_branch_advanced_past_closed_head_is_review(repo: GitRepo) -> None:
    wt = repo.worktree("wt", "feature")
    wt.commit("f", "A\n", "the closed PR's tip")
    head = repo.rev("feature")
    wt.commit("extra", "more\n", "work after the PR was closed")
    result = _classify(repo, "feature", pr=PrInfo(11, PrState.CLOSED, head_sha=head))
    assert isinstance(result, bg.ReviewBranch)
    assert "closed PR #11" in result.reason
    assert "beyond it" in result.reason


def test_open_pr_is_kept(repo: GitRepo) -> None:
    wt = repo.worktree("wt", "feature")
    wt.commit("novel", "unique\n", "work in review")
    result = _classify(repo, "feature", pr=PrInfo(9, PrState.OPEN))
    assert isinstance(result, bg.RetainedBranch)
    assert result.reason == "open PR #9"


def test_default_branch_is_kept(repo: GitRepo) -> None:
    result = _classify(repo, "main")
    assert isinstance(result, bg.RetainedBranch)
    assert result.reason == "default branch"


def test_branch_in_retained_worktree_is_kept(repo: GitRepo) -> None:
    repo.branch("feature")
    holder = RetainedWorktree(Worktree(path=Path("/wt"), branch="feature"), "uncommitted changes", None)
    result = _classify(repo, "feature", holder=holder)
    assert isinstance(result, bg.RetainedBranch)
    assert "retained worktree /wt" in result.reason


def test_branch_in_prunable_worktree_is_prunable(repo: GitRepo) -> None:
    repo.branch("feature")
    repo.commit("later", "1\n", "advance main")
    holder = PrunableWorktree(Worktree(path=Path("/wt"), branch="feature"), "changes already in main", None)
    result = _classify(repo, "feature", holder=holder)
    assert isinstance(result, bg.PrunableBranch)
    assert result.checkout == Path("/wt")


def test_delete_branch_removes_it(repo: GitRepo) -> None:
    repo.branch("feature")
    assert bg.delete_branch(repo.path, "feature") == bg.RemovedBranch("feature")
    assert not repo.has_branch("feature")


def test_delete_branch_fails_when_checked_out(repo: GitRepo) -> None:
    repo.worktree("wt", "feature")
    assert isinstance(bg.delete_branch(repo.path, "feature"), bg.FailedBranch)


def test_cherry_picked_branch_past_the_tree_check_is_prunable(repo: GitRepo) -> None:
    """A branch whose commit landed by cherry-pick, on a main that has since moved past it.

    This is the shape the tree-equality check decays on: main took the same patch and then
    kept editing the same file, so merging the branch in today conflicts and
    `content_in_main` reports it unlanded. Patch equivalence still recognises it, with no PR
    to appeal to.
    """
    wt = repo.worktree("wt", "feature")
    wt.commit("f", "A\n", "feature change")
    repo.run("cherry-pick", repo.rev("feature"))
    repo.commit("f", "B\n", "main advances past the cherry-pick")
    repo.commit("f", "C\n", "and again")

    result = _classify(repo, "feature", pr=None)

    assert isinstance(result, bg.PrunableBranch)
    assert "equivalent already on" in result.reason


def test_branch_with_an_unlanded_commit_stays_review(repo: GitRepo) -> None:
    """One equivalent commit is not enough — an unlanded sibling must still hold it back."""
    wt = repo.worktree("wt", "feature")
    wt.commit("f", "A\n", "landed change")
    repo.run("cherry-pick", repo.rev("feature"))
    wt.commit("g", "never\n", "change main never took")

    result = _classify(repo, "feature", pr=None)

    assert isinstance(result, bg.ReviewBranch)


if __name__ == "__main__":
    pytest_bazel.main()
