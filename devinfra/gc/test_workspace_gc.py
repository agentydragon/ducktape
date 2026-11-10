import asyncio
import pathlib
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
from devinfra.gc.pull_request import PrInfo
from devinfra.gc.removal import RemovalResult, Skipped
from devinfra.gc.work_queue import WorkQueue


def test_main_routes_options_to_default_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_app(*, args: list[str]) -> None:
        calls.append(args)

    monkeypatch.setattr(workspace_gc, "app", fake_app)

    workspace_gc.main(["--no-prs"])

    assert calls == [["all", "--no-prs"]]


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
        delete_foreign_clones=False,
        repo=checkout.path,
        pr_source=workspace_gc._without_prs,
    )
    return asyncio.run(workspace_queue.run(options)).scan


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

    def classify_with_block(worktree: git_repo.Worktree, **kwargs: Any) -> worktree_gc.Classification:
        if worktree.path == slow.path:
            assert removed.wait(10), "the first worktree was not removed while the other was being scanned"
        return classify(worktree, **kwargs)

    def remove_and_signal(*args: Any, **kwargs: Any) -> RemovalResult[pathlib.Path]:
        result = remove(*args, **kwargs)
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
        delete_foreign_clones=False,
        repo=None,
        pr_source=workspace_gc._without_prs,
    )

    async def run_until_interrupted() -> workspace_queue.Report:
        try:
            await workspace_queue.run(options)
        except workspace_queue.QueueInterrupted as error:
            return error.report
        raise AssertionError("run was not interrupted")

    task = asyncio.create_task(run_until_interrupted())
    try:
        assert await asyncio.to_thread(started.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    report = await asyncio.wait_for(task, 10)
    assert len(report.base_removals) == 1
    assert not bases[0].exists()
    assert bases[1].exists()


def test_joint_prune_removes_a_worktree_then_its_base_and_cache_entry(
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
    assert not worktree.path.exists()
    assert not checkout.has_branch("landed")
    assert not base.exists()
    assert not entry.exists()


def test_branch_is_deleted_while_an_unrelated_worktree_is_still_being_scanned(
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = repo.clone("checkout")
    fast = checkout.worktree("a-fast", "fast")
    slow = checkout.worktree("z-slow", "slow")
    root = tmp_path / "output"
    root.mkdir()
    deleted = threading.Event()
    classify = worktree_gc.classify_worktree
    delete = branch_gc.delete_branch

    def classify_with_block(worktree: git_repo.Worktree, **kwargs: Any) -> worktree_gc.Classification:
        if worktree.path == slow.path:
            assert deleted.wait(10), "the fast branch did not advance while the slow worktree was being scanned"
        return classify(worktree, **kwargs)

    def delete_and_signal(repo_path: pathlib.Path, name: str, *, expected_tip: str) -> RemovalResult[str]:
        result = delete(repo_path, name, expected_tip=expected_tip)
        if name == "fast":
            assert not fast.path.exists()
            assert not checkout.has_branch("fast")
            deleted.set()
        return result

    monkeypatch.setattr(output_base_gc, "session_env_output_bases", list)
    monkeypatch.setattr(worktree_gc, "classify_worktree", classify_with_block)
    monkeypatch.setattr(branch_gc, "delete_branch", delete_and_signal)
    with pytest.raises(SystemExit) as exit_info:
        workspace_gc.main(
            ["--repo", str(checkout.path), "--output-user-root", str(root), "--no-prs", "--prune", "--workers", "4"]
        )
    assert exit_info.value.code == 0
    assert deleted.is_set()
    assert not slow.path.exists()


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

    def classify_with_block(worktree: git_repo.Worktree, **kwargs: Any) -> worktree_gc.Classification:
        if worktree.path == slow.path:
            slow_started.set()
            assert cache_deleted.wait(10), "cache cleanup waited for the unrelated worktree scan"
        return classify(worktree, **kwargs)

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

    async def blocked_pr_lookup(_repo: pathlib.Path, _branches: set[str]) -> dict[str, PrInfo]:
        lookup_started.set()
        assert await asyncio.to_thread(base_deleted.wait, 10)
        return {}

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
        delete_foreign_clones=False,
        repo=checkout.path,
        pr_source=blocked_pr_lookup,
    )

    report = await workspace_queue.run(options)

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
        delete_foreign_clones=False,
        repo=None,
        pr_source=workspace_gc._without_prs,
    )

    report = await workspace_queue.run(options)
    assert fast_deleted.is_set()
    assert not fast.exists()
    assert not slow.exists()
    assert len(report.cache_removals) == 2


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
        delete_foreign_clones=False,
        repo=None,
        pr_source=workspace_gc._without_prs,
    )

    await workspace_queue.run(options)
    assert entry_deleted.is_set()
    assert not entry.exists()
    assert not base.exists()


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
    monkeypatch.setattr(
        worktree_gc, "remove_if_still_prunable", lambda _repo, item, **_kwargs: Skipped(item.path, "became busy")
    )

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

    def remove_and_signal(*args: Any, **kwargs: Any) -> RemovalResult[pathlib.Path]:
        result = remove(*args, **kwargs)
        removed.set()
        return result

    def fail_scan(*args: Any, **kwargs: Any) -> None:
        assert removed.wait(10)
        raise RuntimeError("branch scan failed")

    monkeypatch.setattr(output_base_gc, "session_env_output_bases", list)
    monkeypatch.setattr(worktree_gc, "remove_if_still_prunable", remove_and_signal)
    monkeypatch.setattr(workspace_jobs.DiscoverBranches, "run", fail_scan)
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
                "--delete-foreign-clones",
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

    async def __call__(self, repo: pathlib.Path, branches: set[str]) -> dict[str, PrInfo]:
        self.branch_sets.append(set(branches))
        return {}


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
        ("bazel-bases", False, 1),
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
