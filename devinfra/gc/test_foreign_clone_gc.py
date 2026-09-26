from pathlib import Path

import pytest_bazel

from devinfra.gc import foreign_clone_gc as fcg
from devinfra.gc.conftest import GitRepo
from devinfra.gc.pull_request import PrInfo, PrState

_ORIGIN = "https://github.com/acme/widgets"
_OTHER_ORIGIN = "https://github.com/acme/gadgets"


def _foreign_clone(repo: GitRepo, name: str = "foreign") -> GitRepo:
    """A real clone of `repo`, repointed to the same synthetic GitHub origin `repo` itself
    would need to be scanned under — the shape `discover_foreign_clones` is meant to find."""
    clone = repo.clone(name)
    clone.set_origin(_ORIGIN)
    return clone


def test_discover_foreign_clones_finds_matching_clone(repo: GitRepo) -> None:
    repo.set_origin(_ORIGIN)
    foreign = _foreign_clone(repo)

    roots = fcg.discover_foreign_clones({foreign.path}, known_paths=set(), repo=repo.path)

    assert roots == [foreign.path]


def test_discover_foreign_clones_ignores_a_git_dir_that_does_not_open(repo: GitRepo, tmp_path: Path) -> None:
    """A `.git` entry alone isn't proof of a repository — an interrupted `git init` (or a stray
    one, e.g. directly at `/tmp` on a real machine) leaves a `.git` directory that doesn't
    actually open. This must be skipped, not crash the whole scan."""
    repo.set_origin(_ORIGIN)
    broken = tmp_path / "broken"
    (broken / ".git").mkdir(parents=True)
    nested = broken / "some" / "workspace"
    nested.mkdir(parents=True)

    roots = fcg.discover_foreign_clones({nested}, known_paths=set(), repo=repo.path)

    assert roots == []


def test_discover_foreign_clones_excludes_known_paths(repo: GitRepo) -> None:
    repo.set_origin(_ORIGIN)
    foreign = _foreign_clone(repo)

    roots = fcg.discover_foreign_clones({foreign.path}, known_paths={foreign.path}, repo=repo.path)

    assert roots == []


def test_discover_foreign_clones_excludes_different_project(repo: GitRepo) -> None:
    repo.set_origin(_ORIGIN)
    foreign = repo.clone("foreign")
    foreign.set_origin(_OTHER_ORIGIN)

    roots = fcg.discover_foreign_clones({foreign.path}, known_paths=set(), repo=repo.path)

    assert roots == []


def test_discover_foreign_clones_resolves_a_nested_workspace_to_its_repo_root(repo: GitRepo) -> None:
    """A Bazel workspace can be a *subdirectory* of a clone (a vendored third_party checkout
    built as its own Bazel workspace but not its own git repo) — it must resolve up to the
    clone that owns it, not be treated as its own (nonexistent) foreign clone."""
    repo.set_origin(_ORIGIN)
    foreign = _foreign_clone(repo)
    nested = foreign.path / "third_party" / "vendored"
    nested.mkdir(parents=True)

    roots = fcg.discover_foreign_clones({nested}, known_paths=set(), repo=repo.path)

    assert roots == [foreign.path]


def test_discover_foreign_clones_resolves_a_linked_worktree_to_its_hub(repo: GitRepo) -> None:
    """A foreign clone can itself host further linked worktrees; a candidate workspace that is
    one of those must resolve back to the hub, not be treated as its own separate clone."""
    repo.set_origin(_ORIGIN)
    foreign = _foreign_clone(repo)
    nested_wt = foreign.worktree("nested", "spike")

    roots = fcg.discover_foreign_clones({nested_wt.path}, known_paths=set(), repo=repo.path)

    assert roots == [foreign.path]


def test_classify_foreign_clone_prunable_when_every_worktree_is(repo: GitRepo) -> None:
    repo.set_origin(_ORIGIN)
    foreign = _foreign_clone(repo)
    # foreign's own primary checkout is on `main`, an ancestor of itself — trivially prunable.

    result = fcg.classify_foreign_clone(foreign.path, pr_states={})

    assert isinstance(result, fcg.PrunableForeignClone)
    assert result.clone.root == foreign.path


def test_classify_foreign_clone_retained_when_a_member_is_dirty(repo: GitRepo) -> None:
    repo.set_origin(_ORIGIN)
    foreign = _foreign_clone(repo)
    (foreign.path / "scratch").write_text("uncommitted\n")

    result = fcg.classify_foreign_clone(foreign.path, pr_states={})

    assert isinstance(result, fcg.RetainedForeignClone)
    assert str(foreign.path) in result.reason


def test_classify_foreign_clone_review_when_a_nested_worktree_has_unmerged_work(repo: GitRepo) -> None:
    repo.set_origin(_ORIGIN)
    foreign = _foreign_clone(repo)
    nested_wt = foreign.worktree("nested", "spike")
    nested_wt.commit("novel", "unique\n", "unmerged work")

    result = fcg.classify_foreign_clone(foreign.path, pr_states={})

    assert isinstance(result, fcg.ReviewForeignClone)
    assert str(nested_wt.path) in result.reason


def test_classify_foreign_clone_prunable_with_a_closed_pr_on_a_nested_worktree(repo: GitRepo) -> None:
    repo.set_origin(_ORIGIN)
    foreign = _foreign_clone(repo)
    nested_wt = foreign.worktree("nested", "spike")
    nested_wt.commit("novel", "unique\n", "abandoned attempt")

    result = fcg.classify_foreign_clone(foreign.path, pr_states={"spike": PrInfo(11, PrState.CLOSED)})

    assert isinstance(result, fcg.PrunableForeignClone)


def test_prune_foreign_clone_removes_nested_worktrees_then_the_root(repo: GitRepo) -> None:
    repo.set_origin(_ORIGIN)
    foreign = _foreign_clone(repo)
    nested_wt = foreign.worktree("nested", "spike")

    result = fcg.classify_foreign_clone(foreign.path, pr_states={})
    assert isinstance(result, fcg.PrunableForeignClone)

    outcome = fcg.prune_foreign_clone(result.clone)

    assert outcome == fcg.PrunedForeignClone(foreign.path)
    assert not nested_wt.path.exists()
    assert not foreign.path.exists()


if __name__ == "__main__":
    pytest_bazel.main()
