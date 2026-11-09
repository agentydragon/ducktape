import pathlib
import subprocess
import sys
import threading
from io import StringIO

import pytest
import pytest_bazel
from rich.console import Console

from devinfra.gc import repo_contents_cache_gc, workspace_gc, workspace_scan
from devinfra.gc.conftest import GitRepo, link_external, make_base, make_cache_entry
from devinfra.gc.pull_request import PrInfo


def test_main_routes_options_to_default_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_app(*, args: list[str]) -> None:
        calls.append(args)

    monkeypatch.setattr(workspace_gc, "app", fake_app)

    workspace_gc.main(["--no-prs"])

    assert calls == [["all", "--no-prs"]]


# Branch names that look like numbers: `tabulate` rewrites a column whose every cell does.
_NUMERIC_LOOKING_BRANCHES = ["1e5", "2.10", "1.0", "007"]


def _scan_numeric_looking_branches(
    repo: GitRepo, proc: pathlib.Path, mountinfo: pathlib.Path
) -> workspace_scan.WorkspaceScan:
    for index, branch in enumerate(_NUMERIC_LOOKING_BRANCHES):
        repo.worktree(f"wt{index}", branch)  # an empty branch: its worktree and branch are both prunable
    return workspace_scan.scan_workspace(
        repo.path,
        main="main",
        default_branch="main",
        pr_states={},
        session_bases=(),
        proc_root=proc,
        mountinfo_path=mountinfo,
    )


def _cells(report: str, column: int) -> list[str]:
    """One column of a rendered table's rows: not its header line, nor its trailing summary line."""
    return sorted(line.split()[column] for line in report.splitlines()[1:-1])


def test_render_worktrees_prints_numeric_looking_branch_names_verbatim(
    repo: GitRepo, proc: pathlib.Path, mountinfo: pathlib.Path
) -> None:
    scan = _scan_numeric_looking_branches(repo, proc, mountinfo)

    report = workspace_gc.render_worktrees(scan.worktrees, include_kept=False)

    # STATUS, LAST ACTIVITY, WORKTREE, BRANCH: the fourth cell of a row.
    assert _cells(report, 3) == sorted(_NUMERIC_LOOKING_BRANCHES)


def test_render_branches_prints_numeric_looking_branch_names_verbatim(
    repo: GitRepo, proc: pathlib.Path, mountinfo: pathlib.Path
) -> None:
    scan = _scan_numeric_looking_branches(repo, proc, mountinfo)

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
    assert exit_info.value.code == 1
    assert "0 deleted, 2 skipped, 0 failed" in captured.out
    assert "a Bazel command holds the repo contents cache" in captured.err
    for entry in entries:
        assert entry.is_dir()
        assert (entry.name in captured.out) is verbose
        assert (entry.name in captured.err) is verbose
    if not verbose:
        assert "SKIPPED 2 repo-cache entries" in captured.err


async def test_run_bounded_runs_items_concurrently_and_returns_results_in_item_order() -> None:
    both_running = threading.Barrier(2, timeout=5)  # only passes if two items are inside at once

    def function(item: int) -> int:
        both_running.wait()
        return item * 10

    assert await workspace_gc._run_bounded([3, 1], function, workers=2) == [30, 10]


def test_progress_table_aligns_counts_per_phase_without_borders() -> None:
    reporter = workspace_gc._ProgressReporter()
    with reporter:
        reporter.start_phase("worktrees", 12)
        for _ in range(10):
            reporter.record("worktrees", "PRUNE")
        for _ in range(2):
            reporter.record("worktrees", "KEEP")
        reporter.start_phase("branches", 3)
        reporter.record("branches", "REVIEW")
    output = StringIO()
    Console(file=output, width=100, force_terminal=False).print(reporter.render())

    header, worktrees, branches = output.getvalue().splitlines()
    assert header.split() == ["prune", "keep", "review"]
    assert worktrees.split()[:4] == ["worktrees", "10", "2", "0"]
    assert worktrees.split()[-1] == "12/12"
    assert branches.split()[:4] == ["branches", "0", "0", "1"]
    assert branches.split()[-1] == "1/3"
    # Counts line up under their headers.
    assert header.index("review") + len("review") == worktrees.index("0", worktrees.index("2")) + 1
    assert not set("│|┌┐└┘├┤┬┴┼+") & set(header + worktrees + branches)


class _PrLookups:
    """Stands in for `github_prs.pr_states`, recording the branch set of every lookup."""

    def __init__(self) -> None:
        self.branch_sets: list[set[str]] = []

    def __call__(self, repo: pathlib.Path, branches: set[str]) -> dict[str, PrInfo]:
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
    repo: GitRepo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, pr_lookups: _PrLookups
) -> None:
    monkeypatch.setattr(workspace_gc.output_base_gc, "session_env_output_bases", list)
    checkout = repo.clone("checkout")
    based = checkout.worktree("based", "has-a-base")
    checkout.worktree("unbased", "has-no-base")
    root = tmp_path / "output"
    make_base(root, based.path.resolve())

    _scan_command("bazel-bases", checkout, root)

    assert pr_lookups.branch_sets == [{"has-a-base"}]


if __name__ == "__main__":
    pytest_bazel.main()
