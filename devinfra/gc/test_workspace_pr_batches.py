import asyncio
import pathlib
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

import pytest
import pytest_bazel

from devinfra.gc import branch_gc, output_base_gc, workspace_run, workspace_scan, worktree_gc
from devinfra.gc.conftest import GitRepo, make_base
from devinfra.gc.pull_request import PrInfo, PrLookup, PrState

PrSource = Callable[[pathlib.Path, set[str]], Awaitable[PrLookup]]


def _options(
    repo: GitRepo,
    *,
    domains: set[workspace_run.Domain],
    workers: int,
    source: PrSource,
    apply: bool = False,
    root: pathlib.Path | None = None,
) -> workspace_run.Options:
    return workspace_run.Options(
        domains=frozenset(domains),
        root=repo.path if root is None else root,
        session_bases=(),
        workers=workers,
        apply=apply,
        sizes=False,
        repo=repo.path,
        pr_source=source,
    )


async def test_local_worktree_inspection_runs_while_pr_batch_is_blocked(
    repo: GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = repo.clone("checkout")
    worktree = checkout.worktree("topic-wt", "topic")
    lookup_started, local_inspection_started, release_lookup = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked_lookup(_repo: pathlib.Path, _branches: set[str]) -> PrLookup:
        lookup_started.set()
        await release_lookup.wait()
        return PrLookup({})

    classify = worktree_gc.classify_worktree

    async def inspect_local(*args: Any, **kwargs: Any) -> worktree_gc.Classification:
        if args[0].path == worktree.path:
            local_inspection_started.set()
        return await classify(*args, **kwargs)

    monkeypatch.setattr(worktree_gc, "classify_worktree", inspect_local)
    run = workspace_run.Run(
        _options(checkout, domains={workspace_run.Domain.WORKTREES}, workers=2, source=blocked_lookup)
    )
    task = asyncio.create_task(run.run())
    try:
        await asyncio.wait_for(lookup_started.wait(), 5)
        await asyncio.wait_for(local_inspection_started.wait(), 5)
        assert not release_lookup.is_set()
    finally:
        release_lookup.set()

    report = await asyncio.wait_for(task, 5)
    assert report.scan.worktrees


async def test_repository_scan_failure_cancels_and_drains_sibling(
    repo: GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    sibling_started, sibling_finished = asyncio.Event(), asyncio.Event()

    async def failed_facts(_repo: pathlib.Path, _prs: PrLookup) -> workspace_run.RepositoryFacts:
        await sibling_started.wait()
        raise ValueError("local facts failed")

    async def blocked_candidates(_repo: pathlib.Path) -> set[str]:
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            sibling_finished.set()
        return set()

    monkeypatch.setattr(workspace_run, "_repository_facts", failed_facts)
    monkeypatch.setattr(workspace_scan, "pr_branch_candidates", blocked_candidates)

    async def no_lookup(_repo: pathlib.Path, _branches: set[str]) -> PrLookup:
        return PrLookup({})

    run = workspace_run.Run(_options(repo, domains={workspace_run.Domain.WORKTREES}, workers=2, source=no_lookup))
    with pytest.raises(workspace_run.RunError) as error_info:
        await run.run()

    assert isinstance(error_info.value.error, ExceptionGroup)
    assert "local facts failed" in str(error_info.value.error.exceptions[0])
    assert sibling_finished.is_set()


async def test_inspected_worktree_requests_its_current_branch_pr(
    repo: GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = repo.clone("checkout")
    worktree = checkout.worktree("switched-wt", "discovered-branch")
    classify = worktree_gc.classify_worktree
    switched = "current-branch"

    async def classify_current_branch(*args: Any, **kwargs: Any) -> worktree_gc.Classification:
        item = await classify(*args, **kwargs)
        return replace(item, worktree=replace(item.worktree, branch=switched))

    async def lookup(_repo: pathlib.Path, branches: set[str]) -> PrLookup:
        states = {switched: PrInfo(42, PrState.OPEN)} if switched in branches else {}
        return PrLookup(states)

    monkeypatch.setattr(worktree_gc, "classify_worktree", classify_current_branch)
    report = await asyncio.wait_for(
        workspace_run.Run(_options(checkout, domains={workspace_run.Domain.WORKTREES}, workers=2, source=lookup)).run(),
        10,
    )

    item = next(item for item in report.scan.worktrees if item.worktree.path == worktree.path)
    assert isinstance(item, worktree_gc.RetainedWorktree)
    assert item.reason is worktree_gc.RetainedWorktreeReason.OPEN_PR


async def test_one_pr_batch_resolves_while_another_is_blocked(repo: GitRepo) -> None:
    checkout = repo.clone("checkout")
    for index in range(51):
        checkout.branch(f"branch-{index:03d}")

    blocked_started, other_finished, release_blocked, target_resolved = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    calls: list[set[str]] = []

    async def lookup(_repo: pathlib.Path, branches: set[str]) -> PrLookup:
        calls.append(set(branches))
        if "branch-000" in branches:
            blocked_started.set()
            await release_blocked.wait()
        else:
            other_finished.set()
        return PrLookup({})

    class Observer:
        def update(self, progress: Any) -> None:
            if any(item.branch == "branch-050" for item in run.report().scan.branches):
                target_resolved.set()

    run = workspace_run.Run(
        _options(checkout, domains={workspace_run.Domain.BRANCHES}, workers=3, source=lookup), Observer()
    )
    task = asyncio.create_task(run.run())
    try:
        await asyncio.wait_for(blocked_started.wait(), 5)
        await asyncio.wait_for(other_finished.wait(), 5)
        await asyncio.wait_for(target_resolved.wait(), 5)
        resolved = {item.branch for item in run.report().scan.branches}
        assert "branch-000" not in resolved
        assert "branch-050" in resolved
    finally:
        release_blocked.set()

    report = await asyncio.wait_for(task, 10)
    assert len(report.scan.branches) == 52  # 51 topic branches plus main
    assert len(calls) == 2
    assert not (calls[0] & calls[1])
    assert calls[0] | calls[1] == {"main", *(f"branch-{index:03d}" for index in range(51))}


async def test_unknown_pr_state_is_review_and_one_worker_completes(repo: GitRepo) -> None:
    checkout = repo.clone("checkout")
    worktree = checkout.worktree("unknown-wt", "unknown-wt")
    checkout.branch("unheld")

    async def unknown_lookup(_repo: pathlib.Path, branches: set[str]) -> PrLookup:
        return PrLookup({}, branches)

    run = workspace_run.Run(
        _options(
            checkout,
            domains={workspace_run.Domain.WORKTREES, workspace_run.Domain.BRANCHES},
            workers=1,
            source=unknown_lookup,
            apply=True,
        )
    )

    report = await asyncio.wait_for(run.run(), 10)

    worktree_result = next(item for item in report.scan.worktrees if item.worktree.path == worktree.path)
    branch_result = next(item for item in report.scan.branches if item.branch == "unheld")
    assert isinstance(worktree_result, worktree_gc.ReviewWorktree)
    assert worktree_result.reason is worktree_gc.ReviewWorktreeReason.PR_LOOKUP_UNAVAILABLE
    assert isinstance(branch_result, branch_gc.ReviewBranch)
    assert branch_result.reason is branch_gc.ReviewBranchReason.PR_LOOKUP_UNAVAILABLE
    assert worktree.path.exists()


async def test_foreign_clone_referenced_by_multiple_bases_is_inspected_once(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = repo.clone("checkout")
    foreign = checkout.clone("foreign")
    shared_remote = "https://github.com/example/project"
    checkout.set_origin(shared_remote)
    foreign.set_origin(shared_remote)
    output_root = tmp_path / "output"
    make_base(output_root, foreign.path)
    session = make_base(tmp_path / "session", foreign.path, name="bazel-output-base")
    monkeypatch.setattr(output_base_gc, "_TMP_ROOT", tmp_path / "elsewhere")
    discover = workspace_scan.foreign_clone_branch_candidates
    discovered: list[pathlib.Path] = []

    async def discover_once(roots: Any) -> set[str]:
        discovered.extend(roots)
        return await discover(roots)

    monkeypatch.setattr(workspace_scan, "foreign_clone_branch_candidates", discover_once)

    async def no_lookup(_repo: pathlib.Path, _branches: set[str]) -> PrLookup:
        return PrLookup({})

    options = replace(
        _options(
            checkout, domains={workspace_run.Domain.FOREIGN_CLONES}, workers=2, source=no_lookup, root=output_root
        ),
        session_bases=(session,),
    )
    report = await asyncio.wait_for(workspace_run.Run(options).run(), 10)
    assert discovered == [foreign.path]
    assert [item.clone.root for item in report.scan.foreign_clones] == [foreign.path]


async def test_bases_only_run_starts_late_pr_lookup_with_one_worker(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = repo.clone("checkout")
    worktree = checkout.worktree("base-worktree", "base-topic")
    output_root = tmp_path / "output"
    make_base(output_root, worktree.path)
    monkeypatch.setattr(output_base_gc, "session_env_output_bases", list)
    calls: list[set[str]] = []

    async def lookup(_repo: pathlib.Path, branches: set[str]) -> PrLookup:
        calls.append(set(branches))
        return PrLookup({})

    run = workspace_run.Run(
        _options(checkout, domains={workspace_run.Domain.BASES}, workers=1, source=lookup, root=output_root)
    )

    await asyncio.wait_for(run.run(), 10)

    assert calls
    assert any("base-topic" in branches for branches in calls)


async def test_foreign_only_run_starts_late_pr_lookup_with_one_worker(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = repo.clone("checkout")
    foreign = checkout.clone("foreign")
    shared_remote = "https://github.com/example/project"
    checkout.set_origin(shared_remote)
    foreign.set_origin(shared_remote)
    foreign.worktree("foreign-topic-wt", "foreign-topic")
    output_root = tmp_path / "output"
    make_base(output_root, foreign.path)
    monkeypatch.setattr(output_base_gc, "session_env_output_bases", list)
    calls: list[set[str]] = []

    async def lookup(_repo: pathlib.Path, branches: set[str]) -> PrLookup:
        calls.append(set(branches))
        return PrLookup({})

    run = workspace_run.Run(
        _options(checkout, domains={workspace_run.Domain.FOREIGN_CLONES}, workers=1, source=lookup, root=output_root)
    )

    report = await asyncio.wait_for(run.run(), 10)

    assert any("foreign-topic" in branches for branches in calls)
    assert report.scan.foreign_clones


if __name__ == "__main__":
    pytest_bazel.main()
