"""`workspace-gc`: reclaim local development state — Bazel output bases and git worktrees.

Two subcommands sharing the PRUNE/KEEP/REVIEW model:

  * `bazel-bases` — output bases whose workspace is gone (the `output_base_gc` scanner).
  * `worktrees`   — local worktrees whose work is already in the main branch (merged,
    squash/rebase-merged, empty) or merged via a GitHub PR, and are clean + idle.

Neither deletes anything without an explicit `--delete` / `--prune`; both revalidate every
candidate immediately before removing it.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from github import Auth, Github
from tabulate import tabulate

if TYPE_CHECKING:
    from github.PullRequest import PullRequest

from devinfra.gc import output_base_gc, worktree_gc
from devinfra.gc.worktree_gc import (
    Classification,
    PrInfo,
    PrState,
    PrunableWorktree,
    RemovedWorktree,
    RetainedWorktree,
    ReviewWorktree,
    SkippedWorktree,
)

logger = logging.getLogger(__name__)

_PR_RANK = {PrState.MERGED: 3, PrState.OPEN: 2, PrState.CLOSED: 1}
_REMOTE_SLUG_RE = re.compile(r"(?:github\.com[:/])([^/]+/[^/]+?)(?:\.git)?/?$")


def _github_token() -> str | None:
    if token := os.environ.get("GITHUB_TOKEN"):
        return token
    try:
        token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return token or None


def _repo_slug(repo: Path) -> str | None:
    url = worktree_gc._git(repo, "remote", "get-url", "origin", check=False).stdout.strip()
    match = _REMOTE_SLUG_RE.search(url)
    return match.group(1) if match else None


def pr_states(repo: Path, branches: set[str]) -> dict[str, PrInfo]:
    """Most-decisive PR (merged > open > closed) per branch, via PyGithub.

    Returns {} when there is no token, no GitHub remote, or the API is unreachable — the
    caller then classifies on git signals alone.
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
    try:
        owner = slug.split("/", 1)[0]
        gh_repo = Github(auth=Auth.Token(token)).get_repo(slug)
        states: dict[str, PrInfo] = {}
        for branch in branches:
            for pull in gh_repo.get_pulls(state="all", head=f"{owner}:{branch}"):
                info = _pr_info(pull)
                current = states.get(branch)
                if current is None or _PR_RANK[info.state] > _PR_RANK[current.state]:
                    states[branch] = info
        return states
    except Exception:
        logger.warning("PR check skipped: GitHub API error", exc_info=True)
        return {}


def _pr_info(pull: PullRequest) -> PrInfo:
    if pull.merged_at is not None:
        state = PrState.MERGED
    elif pull.state == "open":
        state = PrState.OPEN
    else:
        state = PrState.CLOSED
    return PrInfo(number=pull.number, state=state)


def _short(path: Path) -> str:
    return os.fspath(path).replace(os.fspath(Path.home()), "~")


def _status(item: Classification) -> str:
    if isinstance(item, PrunableWorktree):
        return "PRUNE"
    if isinstance(item, RetainedWorktree):
        return "KEEP"
    return "REVIEW"


def render_worktrees(items: list[Classification], *, include_kept: bool) -> str:
    visible = items if include_kept else [item for item in items if not isinstance(item, RetainedWorktree)]
    rows = [
        [_status(item), _short(item.worktree.path), item.worktree.branch or "(detached)", item.reason]
        for item in visible
    ]
    counts = {
        "prunable": sum(isinstance(item, PrunableWorktree) for item in items),
        "kept": sum(isinstance(item, RetainedWorktree) for item in items),
        "review": sum(isinstance(item, ReviewWorktree) for item in items),
    }
    parts = [tabulate(rows, headers=["STATUS", "WORKTREE", "BRANCH", "DETAIL"], tablefmt="plain")] if rows else []
    parts.append(
        f"Summary: {len(items)} worktrees; {counts['prunable']} prunable, "
        f"{counts['kept']} kept, {counts['review']} review"
    )
    return "\n".join(parts)


def _worktrees_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="workspace-gc worktrees", description="Classify and prune stale worktrees.")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="a path inside the repo (default: cwd)")
    parser.add_argument("--all", action="store_true", help="also show kept worktrees")
    parser.add_argument("--no-prs", action="store_true", help="skip the GitHub PR cross-check (git signals only)")
    parser.add_argument("--prune", action="store_true", help="remove prunable worktrees (revalidated first)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        main = worktree_gc.main_ref(args.repo)
    except worktree_gc.GitError as error:
        print(error, file=sys.stderr)
        return 1
    active = _active_worktree(args.repo)
    main_path = worktree_gc.main_worktree(args.repo)
    branches = (
        set()
        if args.no_prs
        else {wt.branch for wt in worktree_gc.list_worktrees(args.repo) if wt.branch and wt.path != main_path}
    )
    prs = pr_states(args.repo, branches)
    items = worktree_gc.classify_worktrees(args.repo, main=main, pr_states=prs, active_path=active)
    print(render_worktrees(items, include_kept=args.all))
    candidates = [item for item in items if isinstance(item, PrunableWorktree)]
    if not args.prune:
        if candidates:
            print("Dry run only; pass --prune to remove the prunable worktrees.")
        return 0

    results = worktree_gc.remove_prunable_worktrees(args.repo, candidates, main=main, pr_states=prs, active_path=active)
    for result in results:
        if isinstance(result, RemovedWorktree):
            print(f"REMOVED {result.path}")
        elif isinstance(result, SkippedWorktree):
            print(f"SKIPPED {result.path}: {result.reason}", file=sys.stderr)
        else:
            print(f"FAILED {result.path}: {result.error}", file=sys.stderr)
    removed = sum(isinstance(result, RemovedWorktree) for result in results)
    skipped = sum(isinstance(result, SkippedWorktree) for result in results)
    failed = len(results) - removed - skipped
    print(f"Removal: {removed} removed, {skipped} skipped, {failed} failed")
    return int(skipped > 0 or failed > 0)


def _active_worktree(repo: Path) -> Path | None:
    toplevel = worktree_gc._git(repo, "rev-parse", "--show-toplevel", check=False).stdout.strip()
    return Path(toplevel) if toplevel else None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: workspace-gc {bazel-bases,worktrees} [options]", file=sys.stderr)
        return 0 if argv else 2
    command, rest = argv[0], argv[1:]
    if command == "bazel-bases":
        return output_base_gc.main(rest)
    if command == "worktrees":
        return _worktrees_main(rest)
    print(f"workspace-gc: unknown subcommand {command!r} (expected bazel-bases or worktrees)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
