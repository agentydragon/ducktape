"""`workspace-gc`: reclaim local development state — worktrees, branches, Bazel output bases.

The three domains are coupled (a branch checked out in a live worktree can't be deleted; an
output base orphans when its workspace worktree is removed), so one joint scan
(`workspace_scan.scan_workspace`) classifies all three together and the subcommands are views
of that single result:

  * `all`          — the default; every domain, and one `--prune` removes all prunable
                     items in dependency order (worktrees → branches → bases).
  * `worktrees`    — the worktree slice; `--prune` removes prunable worktrees.
  * `bazel-bases`  — the output-base slice; `--delete` removes prunable bases.

Nothing is removed without an explicit flag; every apply re-scans and revalidates each
candidate immediately before removing it. This module owns GitHub access (PR state); the scan
itself is network-free.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Annotated, Any

import httpx
import humanize
import typer
from tabulate import tabulate

from devinfra.gc import branch_gc, git_repo, output_base_gc, workspace_scan, worktree_gc
from devinfra.gc.branch_gc import BranchClassification, FailedBranch, PrunableBranch, RemovedBranch, RetainedBranch
from devinfra.gc.git_repo import GitError
from devinfra.gc.output_base_gc import DeletedBase, FailedBase, PrunableBase, SkippedBase, default_output_user_root
from devinfra.gc.pull_request import PrInfo, PrState
from devinfra.gc.workspace_scan import WorkspaceScan
from devinfra.gc.worktree_gc import (
    Classification,
    FailedWorktree,
    PrunableWorktree,
    RemovedWorktree,
    RetainedWorktree,
    ReviewWorktree,
)

logger = logging.getLogger(__name__)

_PR_RANK = {PrState.MERGED: 3, PrState.OPEN: 2, PrState.CLOSED: 1}
_REMOTE_SLUG_RE = re.compile(r"(?:github\.com[:/])([^/]+/[^/]+?)(?:\.git)?/?$")
_DEFAULT_OUTPUT_USER_ROOT = default_output_user_root()


def _github_token() -> str | None:
    if token := os.environ.get("GITHUB_TOKEN"):
        return token
    try:
        token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return token or None


def _repo_slug(repo: Path) -> str | None:
    url = git_repo.git(repo, "remote", "get-url", "origin", check=False).stdout.strip()
    match = _REMOTE_SLUG_RE.search(url)
    return match.group(1) if match else None


_GRAPHQL_URL = "https://api.github.com/graphql"
_GRAPHQL_BATCH = 50  # aliased pullRequests fields per request; well under GraphQL node limits
_GRAPHQL_STATE = {"MERGED": PrState.MERGED, "OPEN": PrState.OPEN, "CLOSED": PrState.CLOSED}


def _pr_query(owner: str, name: str, branches: list[str]) -> tuple[str, dict[str, str]]:
    """A GraphQL query fetching each branch's PRs under an alias, plus the alias→branch map."""
    alias_to_branch = {f"b{i}": branch for i, branch in enumerate(branches)}
    fields = " ".join(
        f"{alias}: pullRequests(headRefName: {json.dumps(branch)}, first: 5, "
        f"orderBy: {{field: UPDATED_AT, direction: DESC}}) {{ nodes {{ number state headRefOid }} }}"
        for alias, branch in alias_to_branch.items()
    )
    query = f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ {fields} }} }}"
    return query, alias_to_branch


def _most_decisive(nodes: Iterable[Mapping[str, Any]]) -> PrInfo | None:
    """The merged > open > closed PR among `nodes` (a branch's `pullRequests.nodes`)."""
    best: PrInfo | None = None
    for node in nodes:
        info = PrInfo(number=node["number"], state=_GRAPHQL_STATE[node["state"]], head_sha=node["headRefOid"])
        if best is None or _PR_RANK[info.state] > _PR_RANK[best.state]:
            best = info
    return best


def pr_states(repo: Path, branches: set[str]) -> dict[str, PrInfo]:
    """Most-decisive PR (merged > open > closed) per branch, via one batched GraphQL query.

    Aliased `pullRequests(headRefName:)` fields fetch ~50 branches per request instead of a
    REST call per branch — a few round-trips rather than hundreds. `headRefName` matches PR
    records, so a branch whose remote ref was deleted after merge still resolves. Returns {}
    when there is no token, no GitHub remote, or the API is unreachable — the caller then
    classifies on git signals alone.
    """
    if not branches:
        return {}
    slug = _repo_slug(repo)
    token = _github_token()
    if slug is None:
        logger.warning("PR check skipped: origin is not a GitHub remote")
        return {}
    if token is None:
        logger.warning("PR check skipped: no GITHUB_TOKEN and `gh auth token` unavailable")
        return {}
    owner, name = slug.split("/", 1)
    headers = {"Authorization": f"bearer {token}", "User-Agent": "workspace-gc"}
    try:
        states: dict[str, PrInfo] = {}
        with httpx.Client(headers=headers, timeout=30) as client:
            for batch in itertools.batched(sorted(branches), _GRAPHQL_BATCH, strict=False):
                query, alias_to_branch = _pr_query(owner, name, list(batch))
                payload = client.post(_GRAPHQL_URL, json={"query": query}).raise_for_status().json()
                data = payload.get("data")
                if not data or data.get("repository") is None:
                    raise RuntimeError(f"GraphQL returned no data: {payload.get('errors')}")
                repository = data["repository"]
                for alias, branch in alias_to_branch.items():
                    connection = repository.get(alias)
                    if connection is not None and (info := _most_decisive(connection["nodes"])) is not None:
                        states[branch] = info
        return states
    except Exception:
        logger.warning("PR check skipped: GitHub API error", exc_info=True)
        return {}


def _short(path: Path) -> str:
    return os.fspath(path).replace(os.fspath(Path.home()), "~")


def _worktree_status(item: Classification) -> str:
    if isinstance(item, PrunableWorktree):
        return "PRUNE"
    if isinstance(item, RetainedWorktree):
        return "KEEP"
    return "REVIEW"


def _activity(item: Classification) -> str:
    if item.last_activity is None:
        return "?"
    return item.last_activity.astimezone().isoformat(timespec="seconds")


def render_worktrees(items: list[Classification], *, include_kept: bool) -> str:
    visible = items if include_kept else [item for item in items if not isinstance(item, RetainedWorktree)]
    rows = [
        [
            _worktree_status(item),
            _activity(item),
            _short(item.worktree.path),
            item.worktree.branch or "(detached)",
            item.reason,
        ]
        for item in visible
    ]
    counts = {
        "prunable": sum(isinstance(item, PrunableWorktree) for item in items),
        "kept": sum(isinstance(item, RetainedWorktree) for item in items),
        "review": sum(isinstance(item, ReviewWorktree) for item in items),
    }
    headers = ["STATUS", "LAST ACTIVITY", "WORKTREE", "BRANCH", "DETAIL"]
    parts = [tabulate(rows, headers=headers, tablefmt="plain")] if rows else []
    parts.append(
        f"Summary: {len(items)} worktrees; {counts['prunable']} prunable, "
        f"{counts['kept']} kept, {counts['review']} review"
    )
    return "\n".join(parts)


def _branch_status(item: BranchClassification) -> str:
    if isinstance(item, PrunableBranch):
        return "PRUNE"
    if isinstance(item, RetainedBranch):
        return "KEEP"
    return "REVIEW"


def render_branches(items: list[BranchClassification], *, include_kept: bool) -> str:
    visible = items if include_kept else [item for item in items if not isinstance(item, RetainedBranch)]
    rows = [[_branch_status(item), item.branch.name, item.reason] for item in visible]
    counts = {
        "prunable": sum(isinstance(item, PrunableBranch) for item in items),
        "kept": sum(isinstance(item, RetainedBranch) for item in items),
        "review": sum(not isinstance(item, PrunableBranch | RetainedBranch) for item in items),
    }
    headers = ["STATUS", "BRANCH", "DETAIL"]
    parts = [tabulate(rows, headers=headers, tablefmt="plain")] if rows else []
    parts.append(
        f"Summary: {len(items)} branches; {counts['prunable']} prunable, "
        f"{counts['kept']} kept, {counts['review']} review"
    )
    return "\n".join(parts)


def _active_worktree(repo: Path) -> Path | None:
    toplevel = git_repo.git(repo, "rev-parse", "--show-toplevel", check=False).stdout.strip()
    return Path(toplevel) if toplevel else None


def _gather_prs(repo: Path, *, no_prs: bool) -> dict[str, PrInfo]:
    return {} if no_prs else pr_states(repo, workspace_scan.pr_branch_candidates(repo))


def _scan(repo: Path, *, prs: dict[str, PrInfo], output_user_root: Path | None) -> WorkspaceScan:
    return workspace_scan.scan_workspace(
        repo,
        main=git_repo.main_ref(repo),
        default_branch=git_repo.default_branch_name(repo),
        pr_states=prs,
        active_path=_active_worktree(repo),
        output_user_root=output_user_root,
    )


def _apply_worktree_removals(repo: Path, worktrees: list[Classification]) -> int:
    candidates = [item for item in worktrees if isinstance(item, PrunableWorktree)]
    if not candidates:
        return 0
    results = [worktree_gc.remove_worktree(repo, item.worktree.path) for item in candidates]
    for result in results:
        if isinstance(result, RemovedWorktree):
            print(f"REMOVED worktree {_short(result.path)}")
        elif isinstance(result, FailedWorktree):
            print(f"FAILED worktree {_short(result.path)}: {result.error}", file=sys.stderr)
    removed = sum(isinstance(result, RemovedWorktree) for result in results)
    failed = len(results) - removed
    print(f"Worktrees: {removed} removed, {failed} failed")
    return int(failed > 0)


def _apply_branch_deletions(repo: Path, branches: list[BranchClassification]) -> int:
    candidates = [item for item in branches if isinstance(item, PrunableBranch)]
    if not candidates:
        return 0
    results = [branch_gc.delete_branch(repo, item.branch.name) for item in candidates]
    for result in results:
        if isinstance(result, RemovedBranch):
            print(f"DELETED branch {result.name}")
        elif isinstance(result, FailedBranch):
            print(f"FAILED branch {result.name}: {result.error}", file=sys.stderr)
    deleted = sum(isinstance(result, RemovedBranch) for result in results)
    failed = len(results) - deleted
    print(f"Branches: {deleted} deleted, {failed} failed")
    return int(failed > 0)


def _apply_base_deletions(output_user_root: Path) -> int:
    fresh = output_base_gc.scan_output_user_root(output_user_root)
    candidates = [item for item in fresh if isinstance(item, PrunableBase)]
    if not candidates:
        return 0
    free_before = shutil.disk_usage(output_user_root).free
    results = output_base_gc.delete_prunable_bases(candidates)
    free_change = shutil.disk_usage(output_user_root).free - free_before
    for result in results:
        if isinstance(result, DeletedBase):
            print(f"DELETED base {result.path.name}")
        elif isinstance(result, SkippedBase):
            print(f"SKIPPED base {result.path.name}: {result.reason}", file=sys.stderr)
        else:
            quarantine = f"; quarantine={result.quarantine}" if result.quarantine is not None else ""
            print(f"FAILED base {result.path.name}: {result.error}{quarantine}", file=sys.stderr)
    deleted = sum(isinstance(result, DeletedBase) for result in results)
    skipped = sum(isinstance(result, SkippedBase) for result in results)
    failed = sum(isinstance(result, FailedBase) for result in results)
    print(
        f"Bases: {deleted} deleted, {skipped} skipped, {failed} failed; "
        f"free-space change {humanize.naturalsize(free_change, binary=True)}"
    )
    return int(skipped > 0 or failed > 0)


def run_worktrees(repo: Path, *, show_all: bool, no_prs: bool, prune: bool) -> int:
    try:
        prs = _gather_prs(repo, no_prs=no_prs)
        scan = _scan(repo, prs=prs, output_user_root=None)
    except GitError as error:
        print(error, file=sys.stderr)
        return 1
    print(render_worktrees(scan.worktrees, include_kept=show_all))
    if not prune:
        if any(isinstance(item, PrunableWorktree) for item in scan.worktrees):
            print("Dry run only; pass --prune to remove the prunable worktrees.")
        return 0
    return _apply_worktree_removals(repo, _scan(repo, prs=prs, output_user_root=None).worktrees)


def run_bases(repo: Path, *, output_user_root: Path, show_all: bool, no_prs: bool, sizes: bool, delete: bool) -> int:
    # Inspect the bases first: that filesystem pass alone decides every PRUNE/KEEP verdict.
    # Only the "workspace is a prunable worktree" annotation needs git, and only for the few
    # worktrees that are actually a base's workspace — so the PR query is scoped to their
    # branches instead of all of them, and no branch is ever classified.
    try:
        bases = list(output_base_gc.scan_output_user_root(output_user_root))
        prs = {} if no_prs else pr_states(repo, workspace_scan.base_workspace_branches(repo, bases))
        bases = workspace_scan.annotate_bases(
            repo, bases, main=git_repo.main_ref(repo), pr_states=prs, active_path=_active_worktree(repo)
        )
    except (GitError, OSError, RuntimeError) as error:
        print(error, file=sys.stderr)
        return 1
    print(output_base_gc.render_report(bases, include_kept=show_all, include_sizes=sizes))
    if not delete:
        if any(isinstance(item, PrunableBase) for item in bases):
            print("Dry run only; pass --delete to remove the prunable bases.")
        return 0
    return _apply_base_deletions(output_user_root)


def run_all(repo: Path, *, output_user_root: Path, show_all: bool, no_prs: bool, sizes: bool, prune: bool) -> int:
    try:
        prs = _gather_prs(repo, no_prs=no_prs)
        scan = _scan(repo, prs=prs, output_user_root=output_user_root)
    except (GitError, OSError, RuntimeError) as error:
        print(error, file=sys.stderr)
        return 1

    print("# Worktrees")
    print(render_worktrees(scan.worktrees, include_kept=show_all))
    print("\n# Branches")
    print(render_branches(scan.branches, include_kept=show_all))
    print("\n# Bazel output bases")
    print(output_base_gc.render_report(scan.bases, include_kept=show_all, include_sizes=sizes))

    if not prune:
        prunable = (
            any(isinstance(i, PrunableWorktree) for i in scan.worktrees)
            or any(isinstance(i, PrunableBranch) for i in scan.branches)
            or any(isinstance(i, PrunableBase) for i in scan.bases)
        )
        if prunable:
            print("\nDry run only; pass --prune to remove all prunable worktrees, branches, and bases.")
        return 0

    print()
    exit_code = 0
    # Remove worktrees first so branches they hold are freed and their bases orphan.
    exit_code |= _apply_worktree_removals(repo, _scan(repo, prs=prs, output_user_root=None).worktrees)
    exit_code |= _apply_branch_deletions(repo, _scan(repo, prs=prs, output_user_root=None).branches)
    exit_code |= _apply_base_deletions(output_user_root)
    return exit_code


app = typer.Typer(help=__doc__, add_completion=False)

_RepoOption = Annotated[Path, typer.Option("--repo", help="a path inside the repo")]
_RootOption = Annotated[Path, typer.Option("--output-user-root", help="Bazel output user root")]
_AllOption = Annotated[bool, typer.Option("--all", help="also show kept items")]
_NoPrsOption = Annotated[bool, typer.Option("--no-prs", help="skip the GitHub PR cross-check (git signals only)")]
_SizesOption = Annotated[bool, typer.Option("--sizes", help="calculate base sizes with du (potentially slow)")]


@app.callback()
def _configure() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")


@app.command("all")
def _all_command(
    repo: _RepoOption = Path(),
    output_user_root: _RootOption = _DEFAULT_OUTPUT_USER_ROOT,
    show_all: _AllOption = False,
    no_prs: _NoPrsOption = False,
    sizes: _SizesOption = False,
    prune: Annotated[
        bool, typer.Option("--prune", help="remove all prunable worktrees, branches, and output bases")
    ] = False,
) -> None:
    """Classify worktrees, branches, and output bases together (the default command)."""
    raise typer.Exit(
        run_all(repo, output_user_root=output_user_root, show_all=show_all, no_prs=no_prs, sizes=sizes, prune=prune)
    )


@app.command("worktrees")
def _worktrees_command(
    repo: _RepoOption = Path(),
    show_all: _AllOption = False,
    no_prs: _NoPrsOption = False,
    prune: Annotated[bool, typer.Option("--prune", help="remove prunable worktrees (revalidated first)")] = False,
) -> None:
    """The worktree slice of the joint scan."""
    raise typer.Exit(run_worktrees(repo, show_all=show_all, no_prs=no_prs, prune=prune))


@app.command("bazel-bases")
def _bases_command(
    repo: _RepoOption = Path(),
    output_user_root: _RootOption = _DEFAULT_OUTPUT_USER_ROOT,
    show_all: _AllOption = False,
    no_prs: _NoPrsOption = False,
    sizes: _SizesOption = False,
    delete: Annotated[bool, typer.Option("--delete", help="revalidate and remove prunable output bases")] = False,
) -> None:
    """The Bazel output-base slice of the joint scan."""
    raise typer.Exit(
        run_bases(repo, output_user_root=output_user_root, show_all=show_all, no_prs=no_prs, sizes=sizes, delete=delete)
    )


_COMMANDS = {"all", "worktrees", "bazel-bases"}


def _with_default_command(argv: list[str]) -> list[str]:
    """Route a bare invocation (or one that starts with an option) to the `all` command, so
    `workspace-gc` and `workspace-gc --no-prs` behave as `workspace-gc all ...`."""
    if not argv:
        return ["all"]
    if argv[0] in _COMMANDS or argv[0] in ("-h", "--help"):
        return argv
    return ["all", *argv]


def main(argv: list[str] | None = None) -> None:
    """Run the CLI through its importable wheel entry point."""
    app(args=_with_default_command(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
