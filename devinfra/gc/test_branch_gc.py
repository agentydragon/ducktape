from enum import StrEnum
from pathlib import Path

import pygit2
import pytest
import pytest_bazel

from devinfra.gc import branch_gc as bg
from devinfra.gc.conftest import GitRepo
from devinfra.gc.git_repo import Worktree
from devinfra.gc.pull_request import PrInfo, PrState
from devinfra.gc.worktree_gc import RetainedWorktree, RetainedWorktreeReason


async def _classify(
    repo: GitRepo, name: str, *, pr: PrInfo | None = None, holder: bg.Holder = None, default_branch: str = "main"
) -> bg.BranchClassification:
    pg = pygit2.Repository(str(repo.path))
    return await bg.classify_branch(name, pg=pg, main="main", default_branch=default_branch, pr=pr, holder=holder)


async def test_ancestor_branch_is_prunable(repo: GitRepo) -> None:
    repo.branch("feature")
    repo.commit("later", "1\n", "advance main")  # feature is now an ancestor of main
    result = await _classify(repo, "feature")
    assert isinstance(result, bg.PrunableBranch)
    assert result.branch == "feature"
    assert (result.reason, result.main) == (bg.PrunableBranchReason.CONTENT_IN_MAIN, "main")


async def test_empty_branch_is_prunable(repo: GitRepo) -> None:
    repo.branch("feature")
    assert (await _classify(repo, "feature")).reason is bg.PrunableBranchReason.CONTENT_IN_MAIN


async def test_missing_branch_object_is_review(repo: GitRepo) -> None:
    repo.branch("broken")
    missing_oid = "794f4d3258eb250245ed15b44928838ab852e157"
    (repo.path / ".git" / "refs" / "heads" / "broken").write_text(f"{missing_oid}\n")

    result = await _classify(repo, "broken")

    assert isinstance(result, bg.ReviewBranch)
    assert result.reason is bg.ReviewBranchReason.INSPECTION_FAILED
    assert "refs/heads/broken" in bg.describe_reason(result)
    assert repo.has_branch("broken")
    assert (repo.path / ".git" / "refs" / "heads" / "broken").read_text().strip() == missing_oid


async def test_squash_merged_branch_is_prunable(repo: GitRepo) -> None:
    wt = repo.worktree("wt", "feature")
    wt.commit("shared", "same\n", "add on branch")
    repo.commit("shared", "same\n", "same change squashed onto main")
    # Merging feature into main is a no-op — git alone proves the content is already there.
    assert (await _classify(repo, "feature")).reason is bg.PrunableBranchReason.CONTENT_IN_MAIN


async def test_squash_merged_branch_survives_missing_blob(repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert (await _classify(repo, "feature")).reason is bg.PrunableBranchReason.PATCHES_IN_MAIN


async def test_unique_branch_no_pr_is_review(repo: GitRepo) -> None:
    wt = repo.worktree("wt", "feature")
    wt.commit("novel", "unique\n", "unmerged work")
    result = await _classify(repo, "feature")
    assert isinstance(result, bg.ReviewBranch)
    assert (result.reason, result.main) == (bg.ReviewBranchReason.UNMERGED, "main")


async def test_merged_pr_with_content_in_main_is_prunable(repo: GitRepo) -> None:
    repo.branch("feature")
    repo.commit("later", "1\n", "advance main")
    pr = PrInfo(5, PrState.MERGED)
    result = await _classify(repo, "feature", pr=pr)
    assert (result.reason, result.pr) == (bg.PrunableBranchReason.CONTENT_IN_MAIN, pr)


async def test_squash_merged_pr_beyond_git_proof_is_prunable(repo: GitRepo) -> None:
    # main squash-merged two feature commits into one, then moved the same file on past it:
    # the git tree-merge now conflicts, and no single feature commit has a patch-equivalent on
    # main, so only the PR's merged head SHA proves nothing is lost.
    wt = repo.worktree("wt", "feature")
    wt.commit("f", "A\n", "feature change")
    wt.commit("f", "A\nB\n", "feature follow-up")
    head = repo.rev("feature")
    repo.commit("f", "A\nB\n", "squash-merge onto main")
    repo.commit("f", "C\n", "main advances past the squash")
    pr = PrInfo(7, PrState.MERGED, head_sha=head)
    result = await _classify(repo, "feature", pr=pr)
    assert isinstance(result, bg.PrunableBranch)
    assert (result.reason, result.pr, result.main) == (bg.PrunableBranchReason.PR_HEAD_REACHED, pr, "main")


async def test_branch_advanced_past_merged_head_is_review(repo: GitRepo) -> None:
    wt = repo.worktree("wt", "feature")
    wt.commit("f", "A\n", "feature change")
    head = repo.rev("feature")  # the merged tip
    wt.commit("extra", "more\n", "work past the merge")  # feature advances beyond it
    repo.commit("f", "A\n", "squash-merge onto main")
    repo.commit("f", "B\n", "main advances")
    pr = PrInfo(7, PrState.MERGED, head_sha=head)
    result = await _classify(repo, "feature", pr=pr)
    assert isinstance(result, bg.ReviewBranch)
    assert (result.reason, result.pr, result.main) == (bg.ReviewBranchReason.PR_HEAD_EXCEEDED, pr, "main")


async def test_closed_pr_with_nothing_beyond_its_head_is_prunable(repo: GitRepo) -> None:
    # The PR was closed unmerged; nothing was committed on the branch since. This is only a
    # historical-content candidate: deletion preserves its tip and available reflog history.
    wt = repo.worktree("wt", "feature")
    wt.commit("f", "A\n", "abandoned attempt")
    head = repo.rev("feature")
    pr = PrInfo(11, PrState.CLOSED, head_sha=head)
    result = await _classify(repo, "feature", pr=pr)
    assert (result.reason, result.pr) == (bg.PrunableBranchReason.PR_HEAD_REACHED, pr)


async def test_branch_advanced_past_closed_head_is_review(repo: GitRepo) -> None:
    wt = repo.worktree("wt", "feature")
    wt.commit("f", "A\n", "the closed PR's tip")
    head = repo.rev("feature")
    wt.commit("extra", "more\n", "work after the PR was closed")
    pr = PrInfo(11, PrState.CLOSED, head_sha=head)
    result = await _classify(repo, "feature", pr=pr)
    assert (result.reason, result.pr) == (bg.ReviewBranchReason.PR_HEAD_EXCEEDED, pr)


async def test_open_pr_is_kept(repo: GitRepo) -> None:
    wt = repo.worktree("wt", "feature")
    wt.commit("novel", "unique\n", "work in review")
    pr = PrInfo(9, PrState.OPEN)
    result = await _classify(repo, "feature", pr=pr)
    assert (result.reason, result.pr) == (bg.RetainedBranchReason.OPEN_PR, pr)


async def test_branch_in_retained_worktree_is_kept(repo: GitRepo) -> None:
    repo.branch("feature")
    worktree = Worktree(path=Path("/wt"), branch="feature")
    holder = RetainedWorktree(worktree, RetainedWorktreeReason.UNCOMMITTED_CHANGES, None)
    result = await _classify(repo, "feature", holder=holder)
    assert isinstance(result, bg.RetainedBranch)
    assert (result.reason, result.checkout) == (bg.RetainedBranchReason.HELD_BY_WORKTREE, worktree.path)


async def test_cherry_picked_branch_past_the_tree_check_is_prunable(repo: GitRepo) -> None:
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

    result = await _classify(repo, "feature", pr=None)
    assert isinstance(result, bg.PrunableBranch)
    assert (result.reason, result.main) == (bg.PrunableBranchReason.PATCHES_IN_MAIN, "main")


async def test_patch_equivalence_proof_uses_the_captured_branch_oid(
    repo: GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = repo.worktree("wt", "feature")
    wt.commit("f", "A\n", "feature change")
    repo.run("cherry-pick", repo.rev("feature"))
    repo.commit("f", "B\n", "main advances past the cherry-pick")
    captured: list[str] = []
    original = bg.patches_landed_in_main

    async def record_head(path: Path, head: str, main: str) -> bool:
        captured.append(head)
        return await original(path, head, main)

    monkeypatch.setattr(bg, "patches_landed_in_main", record_head)

    result = await _classify(repo, "feature")

    assert isinstance(result, bg.PrunableBranch)
    assert captured == [repo.rev("feature")]
    assert result.tip == captured[0]


async def test_removal_preserves_tip_and_unicode_reflog_history(repo: GitRepo) -> None:
    repo.run("config", "user.name", "GC Test")
    repo.run("config", "user.email", "gc-test@example.test")
    name = "feature/topic"
    repo.run("branch", name, "main")
    old_tip = repo.rev(name)
    repo.commit("later", "1\n", "advance main")
    repo.run("update-ref", "-m", "unicode café history", f"refs/heads/{name}", repo.rev("main"), old_tip)
    candidate = await _classify(repo, name)
    assert isinstance(candidate, bg.PrunableBranch)

    outcome = await bg.remove_prunable_branch(repo.path, candidate)

    assert isinstance(outcome.result, bg.Removed)
    assert outcome.result.subject == name
    assert outcome.recovery_prefix is not None
    assert outcome.recovery_prefix.endswith(f"/{name}")
    assert not repo.has_branch(name)
    prefix = outcome.recovery_prefix
    assert repo.run("rev-parse", f"{prefix}/tip").stdout.strip() == candidate.tip
    assert repo.run("rev-parse", f"{prefix}/history/{old_tip}").stdout.strip() == old_tip


async def test_removal_preserves_oldest_truncated_reflog_predecessor(repo: GitRepo) -> None:
    worktree = repo.worktree("feature-worktree", "feature")
    worktree.commit("unique", "work not in main\n", "unique abandoned commit")
    old_tip = repo.rev("feature")
    repo.commit("main-change", "main\n", "advance main")
    new_tip = repo.rev("main")
    repo.run("worktree", "remove", str(worktree.path))
    repo.run("config", "user.name", "GC Test")
    repo.run("config", "user.email", "gc-test@example.test")
    repo.run("update-ref", "-m", "reset feature to main", "refs/heads/feature", new_tip, old_tip)
    reflog = repo.path / ".git" / "logs" / "refs" / "heads" / "feature"
    latest_entry = reflog.read_bytes().splitlines()[-1]
    reflog.write_bytes(latest_entry + b"\n")
    assert repo.run("rev-parse", "refs/heads/feature@{1}").stdout.strip() == old_tip
    candidate = await _classify(repo, "feature")
    assert isinstance(candidate, bg.PrunableBranch)
    assert candidate.tip == new_tip

    outcome = await bg.remove_prunable_branch(repo.path, candidate)

    assert isinstance(outcome.result, bg.Removed)
    assert outcome.recovery_prefix is not None
    assert repo.run("rev-parse", f"{outcome.recovery_prefix}/history/{old_tip}").stdout.strip() == old_tip


async def test_branch_with_an_unlanded_commit_stays_review(repo: GitRepo) -> None:
    """One equivalent commit is not enough — an unlanded sibling must still hold it back."""
    wt = repo.worktree("wt", "feature")
    wt.commit("f", "A\n", "landed change")
    repo.run("cherry-pick", repo.rev("feature"))
    wt.commit("g", "never\n", "change main never took")

    assert (await _classify(repo, "feature", pr=None)).reason is bg.ReviewBranchReason.UNMERGED


_MAIN = "origin/test-main"
_PR = PrInfo(7421, PrState.MERGED)


def _populated(reason: StrEnum) -> bg.BranchClassification:
    """The classification `reason` belongs to, carrying all the evidence a reason can cite."""
    branch = "feature"
    match reason:
        case bg.PrunableBranchReason():
            return bg.PrunableBranch(branch, reason, _MAIN, "a" * 40, _PR)
        case bg.RetainedBranchReason():
            return bg.RetainedBranch(branch, reason, Path("/wt"), _PR)
        case bg.ReviewBranchReason():
            return bg.ReviewBranch(branch, reason, _MAIN, _PR, detail="test error")
    raise TypeError(reason)


@pytest.mark.parametrize(
    "reason", [*bg.PrunableBranchReason, *bg.RetainedBranchReason, *bg.ReviewBranchReason], ids=str
)
def test_every_reason_is_described(reason: StrEnum) -> None:
    assert bg.describe_reason(_populated(reason))


@pytest.mark.parametrize(
    ("reason", "evidence"),
    [
        pytest.param(bg.PrunableBranchReason.PR_HEAD_REACHED, str(_PR.number), id="pr-head-reached"),
        pytest.param(bg.PrunableBranchReason.CONTENT_IN_MAIN, str(_PR.number), id="content-with-pr"),
        pytest.param(bg.RetainedBranchReason.OPEN_PR, str(_PR.number), id="open-pr"),
        pytest.param(bg.ReviewBranchReason.PR_HEAD_EXCEEDED, str(_PR.number), id="pr-head-exceeded"),
        pytest.param(bg.RetainedBranchReason.HELD_BY_WORKTREE, "/wt", id="holder"),
        pytest.param(bg.PrunableBranchReason.CONTENT_IN_MAIN, _MAIN, id="content-main"),
        pytest.param(bg.PrunableBranchReason.PATCHES_IN_MAIN, _MAIN, id="patches-main"),
        pytest.param(bg.ReviewBranchReason.UNMERGED, _MAIN, id="unmerged-main"),
    ],
)
def test_the_description_names_what_the_reason_cites(reason: StrEnum, evidence: str) -> None:
    assert evidence in bg.describe_reason(_populated(reason))


if __name__ == "__main__":
    pytest_bazel.main()
