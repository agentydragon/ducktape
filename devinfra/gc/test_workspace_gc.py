import asyncio
import os
import pathlib
import signal
import subprocess
import sys
import threading
from io import StringIO
from typing import Any

import pytest
import pytest_bazel
from rich.console import Console

from devinfra.gc import (
    branch_gc,
    git_repo,
    output_base_gc,
    repo_contents_cache_gc,
    workspace_gc,
    workspace_jobs,
    workspace_queue,
    workspace_scan,
    worktree_gc,
)
from devinfra.gc.conftest import GitRepo, link_external, make_base, make_cache_entry
from devinfra.gc.pull_request import PrLookup
from devinfra.gc.removal import RemovalResult, Removed, Skipped
from devinfra.gc.work_queue import Phase, WorkItem, WorkQueue


def test_main_routes_options_to_default_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_app(*, args: list[str]) -> None:
        calls.append(args)

    monkeypatch.setattr(workspace_gc, "app", fake_app)

    workspace_gc.main(["--no-prs"])

    assert calls == [["all", "--no-prs"]]


@pytest.mark.parametrize("flag", ["--delete-foreign-clones", "--delete-foreign-clones=true"])
def test_foreign_clone_delete_flag_fails_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    def fail_if_dispatched(*, args: list[str]) -> None:
        raise AssertionError("must reject the flag before starting any work")

    monkeypatch.setattr(workspace_gc, "app", fail_if_dispatched)
    with pytest.raises(SystemExit) as exit_info:
        workspace_gc.main(["--prune", flag])
    assert exit_info.value.code == 2
    assert "foreign clones are report-only" in capsys.readouterr().err


# Branch names that look like numbers: `tabulate` rewrites a column whose every cell does.
_NUMERIC_LOOKING_BRANCHES = ["1e5", "2.10", "1.0", "007"]


def _scan_numeric_looking_branches(repo: GitRepo) -> workspace_scan.WorkspaceScan:
    checkout = repo.clone("numeric-checkout")
    for index, branch in enumerate(_NUMERIC_LOOKING_BRANCHES):
        checkout.worktree(f"wt{index}", branch)
    options = workspace_queue.Options(
        domains=frozenset({workspace_queue.Domain.WORKTREES, workspace_queue.Domain.BRANCHES}),
        root=repo.path,
        session_bases=(),
        workers=2,
        apply=False,
        sizes=False,
        repo=checkout.path,
        pr_source=workspace_gc._without_prs,
    )
    return asyncio.run(workspace_queue.Run(options).run()).scan


def _cells(report: str, column: int) -> list[str]:
    """One column of a rendered table's rows: not its header line, nor its trailing summary line."""
    return sorted(line.split()[column] for line in report.splitlines()[1:-1])


def test_render_worktrees_prints_numeric_looking_branch_names_verbatim(repo: GitRepo) -> None:
    scan = _scan_numeric_looking_branches(repo)

    report = workspace_gc.render_worktrees(scan.worktrees, include_kept=False)

    # STATUS, LAST ACTIVITY, WORKTREE, BRANCH: the fourth cell of a row.
    assert _cells(report, 3) == sorted(_NUMERIC_LOOKING_BRANCHES)


def test_render_branches_prints_numeric_looking_branch_names_verbatim(repo: GitRepo) -> None:
    scan = _scan_numeric_looking_branches(repo)

    report = workspace_gc.render_branches(scan.branches, include_kept=False)

    # STATUS, BRANCH: the second cell of a row. The default branch is kept, so it is not a row.
    assert _cells(report, 1) == sorted(_NUMERIC_LOOKING_BRANCHES)


def test_joint_prune_frees_the_repo_cache_entries_of_the_bases_it_removes(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain `workspace-gc --prune`: bases go first, then a re-scan frees the entries only they
    linked to, in the same invocation."""
    home = tmp_path / "home"  # holds no session-env bases
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    checkout = repo.clone("checkout")  # the scan needs the `origin/HEAD` a real clone has
    root = tmp_path / "output"
    contents = repo_contents_cache_gc.repo_contents_cache(root)
    kept_entry = make_cache_entry(contents, "a" * 64, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    freed_entry = make_cache_entry(contents, "b" * 64, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    unreferenced_entry = make_cache_entry(contents, "c" * 64, "cccccccc-cccc-cccc-cccc-cccccccccccc")
    kept_base = make_base(root, checkout.path.resolve())
    link_external(kept_base, "kept_repo", kept_entry)
    pruned_base = make_base(root, tmp_path / "gone")
    link_external(pruned_base, "freed_repo", freed_entry)

    with pytest.raises(SystemExit) as exit_info:
        workspace_gc.main(
            ["--repo", str(checkout.path), "--output-user-root", str(root), "--no-prs", "--prune", "--workers", "1"]
        )

    assert exit_info.value.code == 0

    assert not pruned_base.exists()
    assert kept_entry.is_dir()
    assert repo_contents_cache_gc.recorded_inputs_of(kept_entry).is_file()
    for entry in (freed_entry, unreferenced_entry):
        assert not entry.exists()
        assert not repo_contents_cache_gc.recorded_inputs_of(entry).exists()


def test_worktree_pruning_starts_before_other_worktrees_finish_scanning(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = repo.clone("checkout")
    fast = checkout.worktree("a-fast", "landed-fast")
    slow = checkout.worktree("z-slow", "landed-slow")
    removed = threading.Event()
    classify = worktree_gc.classify_worktree
    remove = worktree_gc.remove_if_still_prunable

    async def classify_with_block(worktree: git_repo.Worktree, **kwargs: Any) -> worktree_gc.Classification:
        if worktree.path == slow.path:
            assert await asyncio.to_thread(removed.wait, 10), (
                "the first worktree was not removed while the other was being scanned"
            )
        return await classify(worktree, **kwargs)

    async def remove_and_signal(*args: Any, **kwargs: Any) -> RemovalResult[pathlib.Path]:
        result = await remove(*args, **kwargs)
        if args[1].path == fast.path:
            assert not fast.path.exists()
            removed.set()
        return result

    monkeypatch.setattr(worktree_gc, "classify_worktree", classify_with_block)
    monkeypatch.setattr(worktree_gc, "remove_if_still_prunable", remove_and_signal)
    with pytest.raises(SystemExit) as exit_info:
        workspace_gc.main(["worktrees", "--repo", str(checkout.path), "--no-prs", "--prune", "--workers", "2"])
    assert exit_info.value.code == 0
    assert removed.is_set()
    assert not slow.path.exists()


def test_base_deletion_starts_during_scan_and_preserves_reported_sizes(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = repo.clone("checkout")
    root = tmp_path / "output"
    bases = sorted([make_base(root, tmp_path / "gone-a"), make_base(root, tmp_path / "gone-b")])
    removed = threading.Event()
    inspect = output_base_gc.inspect_output_base
    delete = output_base_gc.delete_prunable_base

    def inspect_with_block(base: pathlib.Path, **kwargs: Any) -> output_base_gc.Inspection:
        if base == bases[1]:
            assert removed.wait(10), "the first base was not removed while the other was being scanned"
        return inspect(base, **kwargs)

    def delete_and_signal(candidate: output_base_gc.PrunableBase, **kwargs: Any) -> RemovalResult[pathlib.Path]:
        result = delete(candidate, **kwargs)
        if candidate.path == bases[0]:
            assert not candidate.path.exists()
            removed.set()
        return result

    monkeypatch.setattr(output_base_gc, "session_env_output_bases", list)
    monkeypatch.setattr(output_base_gc, "inspect_output_base", inspect_with_block)
    monkeypatch.setattr(output_base_gc, "delete_prunable_base", delete_and_signal)
    with pytest.raises(SystemExit) as exit_info:
        workspace_gc.main(
            [
                "bazel-bases",
                "--repo",
                str(checkout.path),
                "--output-user-root",
                str(root),
                "--no-prs",
                "--delete",
                "--sizes",
                "--workers",
                "1",
            ]
        )
    assert exit_info.value.code == 0
    assert all(not base.exists() for base in bases)
    assert "prunable size unavailable" not in capsys.readouterr().out


async def test_interrupt_drains_admitted_removal_without_starting_queued_work(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "output"
    bases = sorted([make_base(root, tmp_path / "gone-a"), make_base(root, tmp_path / "gone-b")])
    started, release = threading.Event(), threading.Event()
    delete = output_base_gc.delete_prunable_base

    def blocking_delete(item: output_base_gc.PrunableBase) -> RemovalResult[pathlib.Path]:
        started.set()
        assert release.wait(10)
        return delete(item)

    monkeypatch.setattr(output_base_gc, "delete_prunable_base", blocking_delete)
    options = workspace_queue.Options(
        domains=frozenset({workspace_queue.Domain.BASES}),
        root=root,
        session_bases=(),
        workers=1,
        apply=True,
        sizes=False,
        repo=None,
        pr_source=workspace_gc._without_prs,
    )

    gc_run = workspace_queue.Run(options)
    task = asyncio.create_task(gc_run.run())
    try:
        assert await asyncio.to_thread(started.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    report = gc_run.report()
    assert len(report.base_removals) == 1
    assert not bases[0].exists()
    assert bases[1].exists()


async def test_interrupt_waits_for_discovery_thread_to_stop(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release, interrupted = threading.Event(), threading.Event(), threading.Event()

    def blocking_scan(
        root: pathlib.Path, *, session_bases: tuple[pathlib.Path, ...]
    ) -> list[tuple[pathlib.Path, output_base_gc.BaseLayout]]:
        started.set()
        assert release.wait(10)
        return []

    stop = WorkQueue.stop

    def signal_stop(queue: WorkQueue[workspace_jobs.Event]) -> list[WorkItem[workspace_jobs.Event]]:
        result = stop(queue)
        interrupted.set()
        return result

    monkeypatch.setattr(output_base_gc, "list_output_bases", blocking_scan)
    monkeypatch.setattr(WorkQueue, "stop", signal_stop)
    options = workspace_queue.Options(
        domains=frozenset({workspace_queue.Domain.BASES}),
        root=tmp_path,
        session_bases=(),
        workers=1,
        apply=False,
        sizes=False,
        repo=None,
        pr_source=workspace_gc._without_prs,
    )

    async def run_until_interrupted() -> None:
        with pytest.raises(asyncio.CancelledError):
            await workspace_queue.Run(options).run()

    task = asyncio.create_task(run_until_interrupted())
    try:
        assert await asyncio.to_thread(started.wait, 10)
        task.cancel()
        assert await asyncio.to_thread(interrupted.wait, 10)
        assert not task.done()
    finally:
        release.set()
    await asyncio.wait_for(task, 10)


async def test_interrupt_during_size_measurement_does_not_delete_base(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "output"
    base = make_base(root, tmp_path / "gone")
    started = asyncio.Event()

    async def blocked_measurement(path: pathlib.Path) -> int | None:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("size measurement should have been cancelled")

    monkeypatch.setattr(workspace_jobs, "_allocated_bytes", blocked_measurement)
    options = workspace_queue.Options(
        domains=frozenset({workspace_queue.Domain.BASES}),
        root=root,
        session_bases=(),
        workers=1,
        apply=True,
        sizes=True,
        repo=None,
        pr_source=workspace_gc._without_prs,
    )
    gc_run = workspace_queue.Run(options)
    task = asyncio.create_task(gc_run.run())
    await asyncio.wait_for(started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert base.is_dir()
    assert not gc_run.report().base_removals


async def test_interrupt_reaps_running_git_read(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fifo = tmp_path / "release"
    os.mkfifo(fifo)
    fake_git = tmp_path / "git"
    fake_git.write_text('#!/bin/sh\nIFS= read -r value < "$WORKSPACE_GC_TEST_FIFO"\n')
    fake_git.chmod(0o755)
    monkeypatch.setenv("WORKSPACE_GC_TEST_FIFO", str(fifo))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    create = asyncio.create_subprocess_exec
    started = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []

    async def monitored(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await create(*args, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", monitored)
    options = workspace_queue.Options(
        domains=frozenset({workspace_queue.Domain.WORKTREES}),
        root=tmp_path,
        session_bases=(),
        workers=1,
        apply=False,
        sizes=False,
        repo=tmp_path,
        pr_source=workspace_gc._without_prs,
    )
    pipe = fifo.open("r+b", buffering=0)

    async def run_until_interrupted() -> None:
        with pytest.raises(asyncio.CancelledError):
            await workspace_queue.Run(options).run()

    task = asyncio.create_task(run_until_interrupted())
    try:
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        await asyncio.wait_for(task, 10)
        assert processes[0].returncode == -signal.SIGKILL
    finally:
        pipe.write(b"release\n")
        pipe.close()


async def test_interrupt_drains_admitted_worktree_removal(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = repo.clone("checkout")
    worktree = checkout.worktree("landed", "landed")
    started, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    remove = worktree_gc.remove_if_still_prunable
    stop = WorkQueue.stop

    async def blocking_remove(*args: Any, **kwargs: Any) -> RemovalResult[pathlib.Path]:
        started.set()
        await release.wait()
        return await remove(*args, **kwargs)

    def signal_stop(queue: WorkQueue[workspace_jobs.Event]) -> list[WorkItem[workspace_jobs.Event]]:
        result = stop(queue)
        stopped.set()
        return result

    monkeypatch.setattr(worktree_gc, "remove_if_still_prunable", blocking_remove)
    monkeypatch.setattr(WorkQueue, "stop", signal_stop)
    options = workspace_queue.Options(
        domains=frozenset({workspace_queue.Domain.WORKTREES}),
        root=tmp_path,
        session_bases=(),
        workers=1,
        apply=True,
        sizes=False,
        repo=checkout.path,
        pr_source=workspace_gc._without_prs,
    )

    gc_run = workspace_queue.Run(options)
    task = asyncio.create_task(gc_run.run())
    try:
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        await asyncio.wait_for(stopped.wait(), 10)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    report = gc_run.report()
    assert report.worktree_removals == [Removed(worktree.path)]
    assert not worktree.path.exists()


async def test_interrupt_drains_admitted_branch_removal_with_recovery_prefix(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = repo.clone("checkout")
    checkout.branch("feature")
    checkout.commit("later", "1\n", "advance main")
    root = tmp_path / "output"
    root.mkdir()
    started, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stop = WorkQueue.stop
    prefix = "refs/workspace-gc/recovery/test/attempt"

    async def blocking_remove(*args: Any, **kwargs: Any) -> branch_gc.BranchRemoval:
        started.set()
        await release.wait()
        return branch_gc.BranchRemoval(Removed("feature"), prefix)

    def signal_stop(queue: WorkQueue[workspace_jobs.Event]) -> list[WorkItem[workspace_jobs.Event]]:
        result = stop(queue)
        stopped.set()
        return result

    monkeypatch.setattr(branch_gc, "remove_prunable_branch", blocking_remove)
    monkeypatch.setattr(WorkQueue, "stop", signal_stop)
    options = workspace_queue.Options(
        domains=frozenset({workspace_queue.Domain.BRANCHES}),
        root=root,
        session_bases=(),
        workers=1,
        apply=True,
        sizes=False,
        repo=checkout.path,
        pr_source=workspace_gc._without_prs,
    )

    gc_run = workspace_queue.Run(options)
    task = asyncio.create_task(gc_run.run())
    try:
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        await asyncio.wait_for(stopped.wait(), 10)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    report = gc_run.report()
    assert report.branch_removals == [Removed("feature")]
    assert report.branch_recovery_refs == {"feature": prefix}


async def test_cli_repeated_sigint_waits_for_mutation_and_reports_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "output"
    base = make_base(root, tmp_path / "gone")
    started, stopped, release = threading.Event(), threading.Event(), threading.Event()
    delete = output_base_gc.delete_prunable_base
    stop_queue = WorkQueue.stop

    def blocking_delete(item: output_base_gc.PrunableBase) -> RemovalResult[pathlib.Path]:
        started.set()
        assert release.wait(10)
        return delete(item)

    def signal_stop(queue: WorkQueue[workspace_jobs.Event]) -> list[WorkItem[workspace_jobs.Event]]:
        result = stop_queue(queue)
        stopped.set()
        return result

    monkeypatch.setattr(output_base_gc, "session_env_output_bases", list)
    monkeypatch.setattr(output_base_gc, "delete_prunable_base", blocking_delete)
    monkeypatch.setattr(WorkQueue, "stop", signal_stop)
    options = workspace_gc._Options(root, False, False, False, 1, True)

    async def interrupt() -> None:
        try:
            assert await asyncio.to_thread(started.wait, 10)
            os.kill(os.getpid(), signal.SIGINT)
            assert await asyncio.to_thread(stopped.wait, 10)
            os.kill(os.getpid(), signal.SIGINT)
            await asyncio.sleep(0)
        finally:
            release.set()

    trigger = asyncio.create_task(interrupt())
    try:
        with pytest.raises(SystemExit) as exit_info:
            await workspace_gc.run(workspace_gc._BASES_COMMAND, options, repo=None, pr_source=workspace_gc._without_prs)
        assert exit_info.value.code == 130
        await trigger
        assert not base.exists()
        assert "Bases: 1 deleted" in capsys.readouterr().out
    finally:
        release.set()
        await trigger


async def test_scan_failure_drains_an_admitted_removal(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "output"
    bases = sorted([make_base(root, tmp_path / "gone-a"), make_base(root, tmp_path / "gone-b")])
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    inspect = output_base_gc.inspect_listed_base
    delete = output_base_gc.delete_prunable_base

    def inspect_with_failure(path: pathlib.Path, **kwargs: Any) -> output_base_gc.Inspection:
        if path == bases[1]:
            assert started.wait(10)
            raise OSError("scan failed")
        return inspect(path, **kwargs)

    def blocking_delete(item: output_base_gc.PrunableBase) -> RemovalResult[pathlib.Path]:
        started.set()
        assert release.wait(10)
        return delete(item)

    monkeypatch.setattr(output_base_gc, "inspect_listed_base", inspect_with_failure)
    monkeypatch.setattr(output_base_gc, "delete_prunable_base", blocking_delete)
    stop = WorkQueue.stop

    def signal_stop(queue: WorkQueue[workspace_jobs.Event]) -> list[WorkItem[workspace_jobs.Event]]:
        result = stop(queue)
        stopped.set()
        return result

    monkeypatch.setattr(WorkQueue, "stop", signal_stop)
    options = workspace_queue.Options(
        domains=frozenset({workspace_queue.Domain.BASES}),
        root=root,
        session_bases=(),
        workers=2,
        apply=True,
        sizes=False,
        repo=None,
        pr_source=workspace_gc._without_prs,
    )

    async def run_until_failure() -> workspace_queue.QueueError:
        try:
            await workspace_queue.Run(options).run()
        except workspace_queue.QueueError as error:
            return error
        raise AssertionError("run did not fail")

    task = asyncio.create_task(run_until_failure())
    try:
        assert await asyncio.to_thread(stopped.wait, 10)
        assert not task.done()
    finally:
        release.set()
    error = await asyncio.wait_for(task, 10)
    assert str(error.error) == "scan failed"
    assert len(error.report.base_removals) == 1
    assert not bases[0].exists()
    assert bases[1].exists()


def test_joint_prune_removes_a_worktree_then_its_base_and_cache_entry(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = repo.clone("checkout")
    worktree = checkout.worktree("landed", "landed")
    root = tmp_path / "output"
    base = make_base(root, worktree.path.resolve())
    entry = make_cache_entry(
        repo_contents_cache_gc.repo_contents_cache(root), "a" * 64, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    )
    link_external(base, "repo", entry)
    monkeypatch.setattr(output_base_gc, "session_env_output_bases", list)
    monkeypatch.setattr(output_base_gc, "_TMP_ROOT", tmp_path / "elsewhere")

    with pytest.raises(SystemExit) as dry_exit:
        workspace_gc.main(["--repo", str(checkout.path), "--output-user-root", str(root), "--no-prs"])
    assert dry_exit.value.code == 0
    assert worktree.path.exists()
    assert base.exists()
    assert entry.exists()

    with pytest.raises(SystemExit) as exit_info:
        workspace_gc.main(["--repo", str(checkout.path), "--output-user-root", str(root), "--no-prs", "--prune"])
    assert exit_info.value.code == 0
    captured = capsys.readouterr()
    assert not worktree.path.exists()
    assert not checkout.has_branch("landed")
    assert not base.exists()
    assert not entry.exists()
    assert "Branches: 1 deleted, 0 skipped, 0 failed" in captured.out
    recovery_line = next(
        line for line in captured.out.splitlines() if line.startswith("Recovery ref for branch landed: ")
    )
    recovery_ref = recovery_line.removeprefix("Recovery ref for branch landed: ")
    assert checkout.run("rev-parse", recovery_ref).stdout.strip() == repo.rev("main")


def test_prune_skips_a_branch_that_moves_after_its_content_scan(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = repo.clone("checkout")
    checkout.branch("feature")
    root = tmp_path / "output"
    root.mkdir()
    classify = branch_gc.classify_branch
    run_git = git_repo.git
    branch_transactions: list[tuple[str, ...]] = []

    async def classify_then_advance(name: str, **kwargs: Any) -> branch_gc.BranchClassification:
        item = await classify(name, **kwargs)
        if name == "feature" and isinstance(item, branch_gc.PrunableBranch):
            before = checkout.rev("feature")
            checkout.commit("later", "new main commit\n", "advance main after the branch scan")
            checkout.run("branch", "-f", "feature", "main")
            assert checkout.rev("feature") != before

        return item

    async def record_branch_transaction(repo_path: pathlib.Path, *args: str, **kwargs: Any) -> Any:
        if args[:2] == ("update-ref", "--stdin"):
            branch_transactions.append(args)
        return await run_git(repo_path, *args, **kwargs)

    monkeypatch.setattr(output_base_gc, "session_env_output_bases", list)
    monkeypatch.setattr(branch_gc, "classify_branch", classify_then_advance)
    monkeypatch.setattr(git_repo, "git", record_branch_transaction)
    with pytest.raises(SystemExit) as exit_info:
        workspace_gc.main(["--repo", str(checkout.path), "--output-user-root", str(root), "--no-prs", "--prune"])
    assert exit_info.value.code == 0
    captured = capsys.readouterr()
    assert checkout.has_branch("feature")
    assert checkout.rev("feature") == checkout.rev("main")
    assert branch_transactions == []
    assert "Branches: 0 deleted, 1 skipped, 0 failed" in captured.out


def test_prune_skips_a_branch_checked_out_after_its_content_scan(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = repo.clone("checkout")
    checkout.branch("feature")
    root = tmp_path / "output"
    root.mkdir()
    classify = branch_gc.classify_branch
    run_git = git_repo.git
    branch_transactions: list[tuple[str, ...]] = []
    late_checkout: GitRepo | None = None

    async def classify_then_checkout(name: str, **kwargs: Any) -> branch_gc.BranchClassification:
        nonlocal late_checkout
        item = await classify(name, **kwargs)
        if name == "feature" and isinstance(item, branch_gc.PrunableBranch):
            late_checkout_path = checkout.path.parent / "late-checkout"
            checkout.run("worktree", "add", str(late_checkout_path), "feature")
            late_checkout = GitRepo(late_checkout_path)
        return item

    async def record_branch_transaction(repo_path: pathlib.Path, *args: str, **kwargs: Any) -> Any:
        if args[:2] == ("update-ref", "--stdin"):
            branch_transactions.append(args)
        return await run_git(repo_path, *args, **kwargs)

    monkeypatch.setattr(output_base_gc, "session_env_output_bases", list)
    monkeypatch.setattr(branch_gc, "classify_branch", classify_then_checkout)
    monkeypatch.setattr(git_repo, "git", record_branch_transaction)
    with pytest.raises(SystemExit) as exit_info:
        workspace_gc.main(["--repo", str(checkout.path), "--output-user-root", str(root), "--no-prs", "--prune"])
    assert exit_info.value.code == 0
    captured = capsys.readouterr()
    assert checkout.has_branch("feature")
    assert late_checkout is not None
    assert late_checkout.path.is_dir()
    assert branch_transactions == []
    assert "Branches: 0 deleted, 1 skipped, 0 failed" in captured.out


def test_prune_restores_a_branch_when_a_symbolic_worktree_head_appears_during_delete(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = repo.clone("checkout")
    checkout.branch("feature")
    expected_tip = checkout.rev("feature")
    root = tmp_path / "output"
    root.mkdir()
    run_git = git_repo.git
    late_checkout: GitRepo | None = None
    dangling_holder_detected = False

    async def create_checkout_before_transaction(repo_path: pathlib.Path, *args: str, **kwargs: Any) -> Any:
        nonlocal late_checkout, dangling_holder_detected
        if args[:2] == ("update-ref", "--stdin") and late_checkout is None:
            late_path = checkout.path.parent / "racing-checkout"
            checkout.run("worktree", "add", "--detach", str(late_path), "feature")
            late_checkout = GitRepo(late_path)
            late_checkout.run("symbolic-ref", "HEAD", "refs/heads/feature")
            outcome = await run_git(repo_path, *args, **kwargs)
            listing = await run_git(repo_path, "worktree", "list", "--porcelain")
            dangling_holder_detected = (
                "branch refs/heads/feature" in listing.stdout and f"HEAD {'0' * len(expected_tip)}" in listing.stdout
            )
            return outcome
        return await run_git(repo_path, *args, **kwargs)

    monkeypatch.setattr(output_base_gc, "session_env_output_bases", list)
    monkeypatch.setattr(git_repo, "git", create_checkout_before_transaction)
    with pytest.raises(SystemExit) as exit_info:
        workspace_gc.main(["--repo", str(checkout.path), "--output-user-root", str(root), "--no-prs", "--prune"])

    assert exit_info.value.code == 0
    captured = capsys.readouterr()
    assert checkout.has_branch("feature")
    assert late_checkout is not None
    assert dangling_holder_detected
    assert late_checkout.run("rev-parse", "HEAD").stdout.strip() == expected_tip
    assert "checkout appeared during deletion" in captured.err
    assert "Recovery ref for branch feature: refs/workspace-gc/recovery/" in captured.out


def test_base_and_cache_cleanup_can_finish_while_worktree_scan_is_blocked(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = repo.clone("checkout")
    slow = checkout.worktree("slow", "slow")
    root = tmp_path / "output"
    base = make_base(root, tmp_path / "gone")
    entry = make_cache_entry(
        repo_contents_cache_gc.repo_contents_cache(root), "a" * 64, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    )
    link_external(base, "repo", entry)
    slow_started, cache_deleted = threading.Event(), threading.Event()
    classify = worktree_gc.classify_worktree
    delete_base = output_base_gc.delete_prunable_base
    empty = repo_contents_cache_gc.empty_trash

    async def classify_with_block(worktree: git_repo.Worktree, **kwargs: Any) -> worktree_gc.Classification:
        if worktree.path == slow.path:
            slow_started.set()
            assert await asyncio.to_thread(cache_deleted.wait, 10), (
                "cache cleanup waited for the unrelated worktree scan"
            )
        return await classify(worktree, **kwargs)

    def delete_after_scan_starts(item: output_base_gc.PrunableBase, **kwargs: Any) -> RemovalResult[pathlib.Path]:
        assert slow_started.wait(10)
        return delete_base(item, **kwargs)

    def empty_and_signal(path: pathlib.Path, trash: pathlib.Path) -> RemovalResult[pathlib.Path]:
        result = empty(path, trash)
        if path == entry:
            assert not entry.exists()
            cache_deleted.set()
        return result

    monkeypatch.setattr(output_base_gc, "session_env_output_bases", list)
    monkeypatch.setattr(worktree_gc, "classify_worktree", classify_with_block)
    monkeypatch.setattr(output_base_gc, "delete_prunable_base", delete_after_scan_starts)
    monkeypatch.setattr(repo_contents_cache_gc, "empty_trash", empty_and_signal)
    with pytest.raises(SystemExit) as exit_info:
        workspace_gc.main(
            ["--repo", str(checkout.path), "--output-user-root", str(root), "--no-prs", "--prune", "--workers", "4"]
        )
    assert exit_info.value.code == 0
    assert cache_deleted.is_set()
    assert not base.exists()


def test_cache_entry_survives_when_one_referencing_base_is_skipped(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = repo.clone("checkout")
    root = tmp_path / "output"
    removed = make_base(root, tmp_path / "gone-a")
    kept = make_base(root, tmp_path / "gone-b")
    entry = make_cache_entry(
        repo_contents_cache_gc.repo_contents_cache(root), "a" * 64, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    )
    link_external(removed, "repo", entry)
    link_external(kept, "repo", entry)
    delete_base = output_base_gc.delete_prunable_base

    def skip_one(item: output_base_gc.PrunableBase, **kwargs: Any) -> RemovalResult[pathlib.Path]:
        if item.path == kept:
            return Skipped(item.path, "busy")
        return delete_base(item, **kwargs)

    monkeypatch.setattr(output_base_gc, "session_env_output_bases", list)
    monkeypatch.setattr(output_base_gc, "delete_prunable_base", skip_one)
    with pytest.raises(SystemExit) as exit_info:
        workspace_gc.main(["--repo", str(checkout.path), "--output-user-root", str(root), "--no-prs", "--prune"])
    assert exit_info.value.code == 0
    assert not removed.exists()
    assert kept.exists()
    assert entry.exists()


async def test_base_cleanup_proceeds_while_pr_lookup_is_waiting(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = repo.clone("checkout")
    root = tmp_path / "output"
    base = make_base(root, tmp_path / "gone")
    lookup_started, base_deleted = threading.Event(), threading.Event()
    delete_base = output_base_gc.delete_prunable_base

    async def blocked_pr_lookup(_repo: pathlib.Path, _branches: set[str]) -> PrLookup:
        lookup_started.set()
        assert await asyncio.to_thread(base_deleted.wait, 10)
        return PrLookup({})

    def delete_and_signal(item: output_base_gc.PrunableBase, **kwargs: Any) -> RemovalResult[pathlib.Path]:
        assert lookup_started.wait(10)
        result = delete_base(item, **kwargs)
        assert not base.exists()
        base_deleted.set()
        return result

    monkeypatch.setattr(output_base_gc, "delete_prunable_base", delete_and_signal)
    options = workspace_queue.Options(
        domains=frozenset({workspace_queue.Domain.WORKTREES, workspace_queue.Domain.BASES}),
        root=root,
        session_bases=(),
        workers=2,
        apply=True,
        sizes=False,
        repo=checkout.path,
        pr_source=blocked_pr_lookup,
    )

    report = await workspace_queue.Run(options).run()

    assert base_deleted.is_set()
    assert report.base_removals
    assert not base.exists()


async def test_cache_deletion_starts_while_another_bucket_is_still_being_scanned(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "output"
    contents = repo_contents_cache_gc.repo_contents_cache(root)
    fast = make_cache_entry(contents, "a" * 64, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    slow = make_cache_entry(contents, "b" * 64, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    slow_started, fast_deleted = threading.Event(), threading.Event()
    list_bucket = repo_contents_cache_gc.list_cache_bucket
    empty = repo_contents_cache_gc.empty_trash

    def list_with_block(bucket: pathlib.Path) -> list[pathlib.Path | repo_contents_cache_gc.ReviewEntry]:
        if bucket == slow.parent:
            slow_started.set()
            assert fast_deleted.wait(10), "cache deletion did not overlap another bucket scan"
        elif bucket == fast.parent:
            assert slow_started.wait(10)
        return list_bucket(bucket)

    def empty_and_signal(path: pathlib.Path, trash: pathlib.Path) -> RemovalResult[pathlib.Path]:
        result = empty(path, trash)
        if path == fast:
            fast_deleted.set()
        return result

    monkeypatch.setattr(output_base_gc, "session_env_output_bases", list)
    monkeypatch.setattr(repo_contents_cache_gc, "list_cache_bucket", list_with_block)
    monkeypatch.setattr(repo_contents_cache_gc, "empty_trash", empty_and_signal)
    options = workspace_queue.Options(
        domains=frozenset({workspace_queue.Domain.REPO_CACHE}),
        root=root,
        session_bases=(),
        workers=2,
        apply=True,
        sizes=False,
        repo=None,
        pr_source=workspace_gc._without_prs,
    )

    report = await workspace_queue.Run(options).run()
    assert fast_deleted.is_set()
    assert not fast.exists()
    assert not slow.exists()
    assert len(report.cache_removals) == 2


async def test_worktree_removal_does_not_wait_for_foreign_clone_discovery(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = repo.clone("checkout")
    checkout.set_origin("https://github.com/example/project")
    worktree = checkout.worktree("landed", "landed")
    foreign = checkout.clone("foreign")
    foreign.set_origin("https://github.com/example/project")
    root = tmp_path / "output"
    make_base(root, foreign.path)
    started, removed = asyncio.Event(), asyncio.Event()
    discover = workspace_jobs.foreign_clone_gc.discover_foreign_clones
    remove = worktree_gc.remove_if_still_prunable

    async def slow_discovery(*args: Any, **kwargs: Any) -> list[pathlib.Path]:
        started.set()
        await asyncio.wait_for(removed.wait(), 10)
        return await discover(*args, **kwargs)

    async def remove_after_discovery(*args: Any, **kwargs: Any) -> RemovalResult[pathlib.Path]:
        await asyncio.wait_for(started.wait(), 10)
        result = await remove(*args, **kwargs)
        removed.set()
        return result

    monkeypatch.setattr(output_base_gc, "_TMP_ROOT", tmp_path / "elsewhere")
    monkeypatch.setattr(workspace_jobs.foreign_clone_gc, "discover_foreign_clones", slow_discovery)
    monkeypatch.setattr(worktree_gc, "remove_if_still_prunable", remove_after_discovery)
    options = workspace_queue.Options(
        domains=frozenset({workspace_queue.Domain.WORKTREES, workspace_queue.Domain.FOREIGN_CLONES}),
        root=root,
        session_bases=(),
        workers=2,
        apply=True,
        sizes=False,
        repo=checkout.path,
        pr_source=workspace_gc._without_prs,
    )

    report = await workspace_queue.Run(options).run()
    assert report.worktree_removals == [Removed(worktree.path)]
    assert report.scan.foreign_clones


async def test_unreferenced_cache_entry_is_deleted_while_base_inspection_is_blocked(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "output"
    base = make_base(root, tmp_path / "gone")
    contents = repo_contents_cache_gc.repo_contents_cache(root)
    entry = make_cache_entry(contents, "a" * 64, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    base_started, entry_deleted = threading.Event(), threading.Event()
    inspect = output_base_gc.inspect_listed_base
    empty = repo_contents_cache_gc.empty_trash

    def inspect_with_block(path: pathlib.Path, **kwargs: Any) -> output_base_gc.Inspection:
        base_started.set()
        assert entry_deleted.wait(10), "cache cleanup waited for an unrelated base inspection"
        return inspect(path, **kwargs)

    def empty_and_signal(path: pathlib.Path, trash: pathlib.Path) -> RemovalResult[pathlib.Path]:
        assert base_started.wait(10)
        result = empty(path, trash)
        entry_deleted.set()
        return result

    monkeypatch.setattr(output_base_gc, "session_env_output_bases", list)
    monkeypatch.setattr(output_base_gc, "inspect_listed_base", inspect_with_block)
    monkeypatch.setattr(repo_contents_cache_gc, "empty_trash", empty_and_signal)
    options = workspace_queue.Options(
        domains=frozenset({workspace_queue.Domain.BASES, workspace_queue.Domain.REPO_CACHE}),
        root=root,
        session_bases=(),
        workers=2,
        apply=True,
        sizes=False,
        repo=None,
        pr_source=workspace_gc._without_prs,
    )

    await workspace_queue.Run(options).run()
    assert entry_deleted.is_set()
    assert not entry.exists()
    assert not base.exists()


async def test_progress_counts_cache_entries_waiting_for_a_batch(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "output"
    contents = repo_contents_cache_gc.repo_contents_cache(root)
    for char in "abc":
        make_cache_entry(contents, char * 64, f"{char * 8}-{char * 4}-{char * 4}-{char * 4}-{char * 12}")
    release = threading.Event()
    trash = repo_contents_cache_gc.trash_prunable_entries

    def trash_after_waiting(*args: Any, **kwargs: Any) -> repo_contents_cache_gc.TrashedEntries:
        assert release.wait(10), "waiting cache entries were not reported"
        return trash(*args, **kwargs)

    class Observer:
        max_waiting = 0

        def update(self, queue: WorkQueue[workspace_jobs.Event], *, waiting: int) -> None:
            self.max_waiting = max(self.max_waiting, waiting)
            if waiting:
                release.set()

    monkeypatch.setattr(output_base_gc, "session_env_output_bases", list)
    monkeypatch.setattr(repo_contents_cache_gc, "trash_prunable_entries", trash_after_waiting)
    observer = Observer()
    options = workspace_queue.Options(
        domains=frozenset({workspace_queue.Domain.REPO_CACHE}),
        root=root,
        session_bases=(),
        workers=3,
        apply=True,
        sizes=False,
        repo=None,
        pr_source=workspace_gc._without_prs,
    )

    report = await workspace_queue.Run(options, observer).run()
    assert observer.max_waiting > 0
    assert len(report.cache_removals) == 3


async def test_progress_counts_cache_entry_waiting_for_worktree_and_base_removal(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = repo.clone("checkout")
    worktree = checkout.worktree("landed", "landed")
    root = tmp_path / "output"
    base = make_base(root, worktree.path.resolve())
    entry = make_cache_entry(
        repo_contents_cache_gc.repo_contents_cache(root), "a" * 64, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    )
    link_external(base, "repo", entry)
    release = threading.Event()
    remove = worktree_gc.remove_if_still_prunable

    async def remove_after_waiting(*args: Any, **kwargs: Any) -> RemovalResult[pathlib.Path]:
        assert await asyncio.to_thread(release.wait, 10), "cache dependency was not reported as waiting"
        return await remove(*args, **kwargs)

    class Observer:
        saw_dependency = False

        def update(self, queue: WorkQueue[workspace_jobs.Event], *, waiting: int) -> None:
            phases = queue.phases
            if (
                waiting
                and phases.get("remove worktrees", Phase()).running
                and phases.get("inspect bases", Phase()).finished
                and phases.get("inspect repo-cache entries", Phase()).finished
            ):
                self.saw_dependency = True
                release.set()

    monkeypatch.setattr(output_base_gc, "session_env_output_bases", list)
    monkeypatch.setattr(output_base_gc, "_TMP_ROOT", tmp_path / "elsewhere")
    monkeypatch.setattr(worktree_gc, "remove_if_still_prunable", remove_after_waiting)
    observer = Observer()
    options = workspace_queue.Options(
        domains=frozenset(
            {workspace_queue.Domain.WORKTREES, workspace_queue.Domain.BASES, workspace_queue.Domain.REPO_CACHE}
        ),
        root=root,
        session_bases=(),
        workers=4,
        apply=True,
        sizes=False,
        repo=checkout.path,
        pr_source=workspace_gc._without_prs,
    )

    await workspace_queue.Run(options, observer).run()
    assert observer.saw_dependency
    assert not entry.exists()


def test_queue_discovers_and_deletes_session_output_bases(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = repo.clone("checkout")
    root = tmp_path / "output"
    root.mkdir()
    session = make_base(tmp_path / "session", tmp_path / "gone", name="bazel-output-base")
    monkeypatch.setattr(output_base_gc, "session_env_output_bases", lambda: [session])

    with pytest.raises(SystemExit) as exit_info:
        workspace_gc.main(
            ["bazel-bases", "--repo", str(checkout.path), "--output-user-root", str(root), "--no-prs", "--delete"]
        )

    assert exit_info.value.code == 0
    assert not session.exists()


def test_skipped_base_is_not_retried_after_the_scan(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = repo.clone("checkout")
    root = tmp_path / "output"
    base = make_base(root, tmp_path / "gone")
    attempts: list[pathlib.Path] = []

    def skip(candidate: output_base_gc.PrunableBase) -> Skipped[pathlib.Path]:
        attempts.append(candidate.path)
        return Skipped(candidate.path, "busy")

    monkeypatch.setattr(output_base_gc, "session_env_output_bases", list)
    monkeypatch.setattr(output_base_gc, "delete_prunable_base", skip)
    with pytest.raises(SystemExit) as exit_info:
        workspace_gc.main(["--repo", str(checkout.path), "--output-user-root", str(root), "--no-prs", "--prune"])
    assert exit_info.value.code == 0
    assert attempts == [base]
    assert base.is_dir()


def test_skipped_worktree_keeps_its_branch_without_failing_the_run(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = repo.clone("checkout")
    worktree = checkout.worktree("landed", "landed")
    root = tmp_path / "output"
    root.mkdir()
    monkeypatch.setattr(output_base_gc, "session_env_output_bases", list)

    async def skipped(_repo: pathlib.Path, item: git_repo.Worktree, **_kwargs: Any) -> RemovalResult[pathlib.Path]:
        return Skipped(item.path, "became busy")

    monkeypatch.setattr(worktree_gc, "remove_if_still_prunable", skipped)

    with pytest.raises(SystemExit) as exit_info:
        workspace_gc.main(["--repo", str(checkout.path), "--output-user-root", str(root), "--no-prs", "--prune"])

    assert exit_info.value.code == 0
    assert worktree.path.exists()
    assert checkout.has_branch("landed")


def test_scan_failure_reports_removals_that_already_completed(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = repo.clone("checkout")
    worktree = checkout.worktree("landed", "landed")
    root = tmp_path / "output"
    root.mkdir()
    removed = threading.Event()
    remove = worktree_gc.remove_if_still_prunable

    async def remove_and_signal(*args: Any, **kwargs: Any) -> RemovalResult[pathlib.Path]:
        result = await remove(*args, **kwargs)
        removed.set()
        return result

    async def fail_scan(*args: Any, **kwargs: Any) -> None:
        assert await asyncio.to_thread(removed.wait, 10)
        raise RuntimeError("branch scan failed")

    monkeypatch.setattr(output_base_gc, "session_env_output_bases", list)
    monkeypatch.setattr(worktree_gc, "remove_if_still_prunable", remove_and_signal)
    monkeypatch.setattr(workspace_jobs.DiscoverBranches, "run_async", fail_scan)
    with pytest.raises(SystemExit) as exit_info:
        workspace_gc.main(["--repo", str(checkout.path), "--output-user-root", str(root), "--no-prs", "--prune"])

    output = capsys.readouterr()
    assert exit_info.value.code == 1
    assert not worktree.path.exists()
    assert "Worktrees: 1 removed" in output.out
    assert "branch scan failed" in output.err


@pytest.mark.parametrize("workers", [1, 4])
def test_prune_continues_past_missing_git_objects(
    repo: GitRepo,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    workers: int,
) -> None:
    checkout = repo.clone("checkout")
    checkout.set_origin("https://github.com/acme/widgets")
    bad_worktree = checkout.worktree("bad-worktree", "broken")
    # Create this commit outside libgit2 so its object cache cannot mask removal.
    bad_worktree.run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "unmerged work")
    good_worktree = checkout.worktree("good-worktree", "landed")
    foreign = checkout.clone("bad-clone")
    foreign.set_origin("https://github.com/acme/widgets")
    missing_oid = foreign.run("rev-parse", "HEAD").stdout.strip()
    (foreign.path / ".git" / "objects" / missing_oid[:2] / missing_oid[2:]).unlink()
    # Exercise libgit2 with a real missing object, alongside the injected status
    # failures in test_worktree_gc. Status can itself bypass missing HEAD objects.
    with pytest.raises(KeyError, match=missing_oid):
        foreign.pg()[missing_oid]
    missing_oid = bad_worktree.run("rev-parse", "HEAD").stdout.strip()
    (checkout.path / ".git" / "objects" / missing_oid[:2] / missing_oid[2:]).unlink()
    with pytest.raises(KeyError, match=missing_oid):
        bad_worktree.pg()[missing_oid]
    root = tmp_path / "output"
    make_base(root, foreign.path)
    orphan = make_base(root, tmp_path / "gone")
    monkeypatch.setattr(workspace_gc.output_base_gc, "session_env_output_bases", list)

    with pytest.raises(SystemExit) as exit_info:
        workspace_gc.main(
            [
                "--repo",
                str(checkout.path),
                "--output-user-root",
                str(root),
                "--no-prs",
                "--prune",
                "--workers",
                str(workers),
            ]
        )

    assert exit_info.value.code == 0
    captured = capsys.readouterr()
    assert "REVIEW" in captured.out
    assert "cannot inspect" in captured.out
    assert bad_worktree.path.name in captured.out
    assert foreign.path.name in captured.out
    assert "Traceback" not in captured.err
    assert bad_worktree.path.is_dir()
    assert checkout.has_branch("broken")
    assert foreign.path.is_dir()
    assert not good_worktree.path.exists()
    assert not checkout.has_branch("landed")
    assert "Branches: 1 deleted, 0 skipped, 0 failed" in captured.out
    assert not orphan.exists()


@pytest.mark.parametrize("error", [KeyError("missing object"), PermissionError("unreadable repository")])
def test_main_reports_global_inspection_errors_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    def fail(*, args: list[str]) -> None:
        raise error

    monkeypatch.setattr(workspace_gc, "app", fail)
    with pytest.raises(SystemExit) as exit_info:
        workspace_gc.main([])

    assert exit_info.value.code == 1
    assert str(error) in capsys.readouterr().err


@pytest.mark.parametrize("verbose", [False, True])
def test_busy_repo_cache_reports_skipped_entries_compactly(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], verbose: bool
) -> None:
    root = tmp_path / "output"
    contents = repo_contents_cache_gc.repo_contents_cache(root)
    entries = [
        make_cache_entry(contents, char * 64, f"{char * 8}-{char * 4}-{char * 4}-{char * 4}-{char * 12}")
        for char in "ab"
    ]
    (contents / "gc_lock").touch()
    monkeypatch.setattr(workspace_gc.output_base_gc, "session_env_output_bases", list)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,sys; f=open(sys.argv[1], 'r+'); fcntl.lockf(f, fcntl.LOCK_SH); print('locked', flush=True); sys.stdin.read(1)",
            str(contents / "gc_lock"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline() == "locked\n"
        with pytest.raises(SystemExit) as exit_info:
            workspace_gc.main(
                [
                    "repo-cache",
                    "--output-user-root",
                    str(root),
                    "--delete",
                    "--workers",
                    "1",
                    *(["-v"] if verbose else []),
                ]
            )
    finally:
        holder.communicate("x", timeout=5)
    captured = capsys.readouterr()
    assert exit_info.value.code == 0  # skipped entries are reported but do not fail the run
    assert "0 deleted, 2 skipped, 0 failed" in captured.out
    assert "a Bazel command holds the repo contents cache" in captured.err
    for entry in entries:
        assert entry.is_dir()
        assert (entry.name in captured.out) is verbose
        assert (entry.name in captured.err) is verbose
    if not verbose:
        assert "SKIPPED 2 repo-cache entries" in captured.err


def test_queue_progress_reports_discovered_and_waiting_work(tmp_path: pathlib.Path) -> None:
    queue: WorkQueue[workspace_jobs.Event] = WorkQueue(workers=2)
    queue.submit(workspace_jobs.DiscoverBases(tmp_path, ()))
    reporter = workspace_gc._QueueReporter()
    reporter.update(queue, waiting=2)
    output = StringIO()
    Console(file=output, width=160, force_terminal=False).print(reporter.render())

    rendered = output.getvalue()
    assert "ready" in rendered
    assert "active" in rendered
    assert "finished" in rendered
    assert "2 waiting" in rendered
    assert "discover" in rendered


async def test_queue_progress_denominator_grows_when_new_work_is_discovered(tmp_path: pathlib.Path) -> None:
    queue: WorkQueue[workspace_jobs.Event] = WorkQueue(workers=1)
    reporter = workspace_gc._QueueReporter()
    queue.submit(workspace_jobs.DiscoverBases(tmp_path, ()))
    async with asyncio.TaskGroup() as tasks:
        queue.start(tasks)
        await queue.next()
    reporter.update(queue, waiting=0)
    first = StringIO()
    Console(file=first, width=160, force_terminal=False).print(reporter.render())
    assert "1/1" in first.getvalue()

    queue.submit(workspace_jobs.DiscoverBases(tmp_path, ()))
    queue.submit(workspace_jobs.DiscoverBases(tmp_path, ()))
    reporter.update(queue, waiting=0)
    later = StringIO()
    Console(file=later, width=160, force_terminal=False).print(reporter.render())
    assert "1/3" in later.getvalue()


class _PrLookups:
    """Stands in for `github_prs.pr_states`, recording the branch set of every lookup."""

    def __init__(self) -> None:
        self.branch_sets: list[set[str]] = []

    async def __call__(self, repo: pathlib.Path, branches: set[str]) -> PrLookup:
        self.branch_sets.append(set(branches))
        return PrLookup({})


@pytest.fixture
def pr_lookups(monkeypatch: pytest.MonkeyPatch) -> _PrLookups:
    lookups = _PrLookups()
    monkeypatch.setattr(workspace_gc.github_prs, "pr_states", lookups)
    return lookups


def _scan_command(command: str, checkout: GitRepo, root: pathlib.Path, *flags: str) -> None:
    """Run one subcommand as a dry run against `checkout`. The repo-cache view takes no repo; the
    worktrees view takes no output root."""
    repo_flags = [] if command == "repo-cache" else ["--repo", str(checkout.path)]
    root_flags = [] if command == "worktrees" else ["--output-user-root", str(root)]
    with pytest.raises(SystemExit) as exit_info:
        workspace_gc.main([command, *repo_flags, *root_flags, "--workers", "1", *flags])
    assert exit_info.value.code == 0


@pytest.mark.parametrize(
    ("command", "no_prs", "lookups"),
    [
        ("all", False, 1),
        ("worktrees", False, 1),
        ("bazel-bases", False, 0),
        ("repo-cache", False, 0),
        ("all", True, 0),
        ("worktrees", True, 0),
        ("bazel-bases", True, 0),
    ],
)
def test_each_run_asks_for_pr_state_at_most_once_and_never_under_no_prs(
    repo: GitRepo,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    pr_lookups: _PrLookups,
    command: str,
    no_prs: bool,
    lookups: int,
) -> None:
    monkeypatch.setattr(workspace_gc.output_base_gc, "session_env_output_bases", list)
    checkout = repo.clone("checkout")
    root = tmp_path / "output"
    root.mkdir()

    _scan_command(command, checkout, root, *(["--no-prs"] if no_prs else []))

    assert len(pr_lookups.branch_sets) == lookups


def test_bazel_bases_view_looks_up_only_the_branches_of_base_workspaces(
    repo: GitRepo,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    pr_lookups: _PrLookups,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(workspace_gc.output_base_gc, "session_env_output_bases", list)
    monkeypatch.setattr(output_base_gc, "_TMP_ROOT", tmp_path / "elsewhere")
    checkout = repo.clone("checkout")
    based = checkout.worktree("based", "has-a-base")
    checkout.worktree("unbased", "has-no-base")
    root = tmp_path / "output"
    make_base(root, based.path.resolve())

    _scan_command("bazel-bases", checkout, root, "--all")

    assert pr_lookups.branch_sets == [{"has-a-base"}]
    assert "workspace is a prunable worktree" in capsys.readouterr().out


if __name__ == "__main__":
    pytest_bazel.main()
