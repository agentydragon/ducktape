from __future__ import annotations

from pathlib import Path

import pygit2

from wt.server.git_manager import GitManager, NoSuchRefError
from wt.shared.protocol import CommitInfo


class RepoStatus:
    def __init__(self, git_manager: GitManager, config):
        self.git_manager = git_manager
        self.config = config

    def summarize_status(self, worktree_path: Path) -> tuple[CommitInfo | None, tuple[int, int], str]:
        try:
            repo = self.git_manager.get_repo(worktree_path)
        except (pygit2.GitError, OSError, ValueError):
            return None, (0, 0), ""
        branch_name = repo.head.shorthand or ""
        commit_info: CommitInfo | None
        try:
            commit_info = self.git_manager.get_commit_info("HEAD", worktree_path)
        except (NoSuchRefError, pygit2.GitError, KeyError, ValueError):
            commit_info = None

        # Ahead/behind computation
        #
        # Why compute from the worktree repo first?
        # - The branch checked out in a worktree can diverge from the branch record in the main repo
        #   (e.g. refs not yet updated in main, or local-only branches). Using the worktree repo's HEAD
        #   as the source of truth ensures we compare the exact commit currently checked out in that worktree.
        # - We still need an upstream reference (e.g. "main"). Prefer the worktree repo's ref if present,
        #   and fall back to the main repo's ref when the worktree doesn't carry that reference.
        # - We call ahead_behind on the main repo because worktrees share the object database; this guarantees
        #   both OIDs are resolvable even if the worktree's ref namespace is sparse.
        #
        # TODO(mpokorny): Add focused tests for detached HEAD handling (worktree repo):
        # ensure zero ahead/behind and no crashes
        # TODO(mpokorny): Add tests for missing upstream refs (in worktree and/or main) verifying
        # fallback to main and zero result
        ahead_behind = (0, 0)
        if (
            worktree_path != self.config.main_repo
            and not repo.head_is_detached
            and branch_name
            and branch_name != "HEAD"
        ):
            try:
                main_repo = self.git_manager.get_repo(self.config.main_repo)

                # Resolve local tip OID from the worktree repo first; fall back to HEAD
                try:
                    local_ref = repo.lookup_reference(f"refs/heads/{branch_name}")
                    local_id = local_ref.target
                except KeyError:
                    local_id = repo.head.target

                # Resolve upstream tip OID: prefer worktree repo; fall back to main repo
                try:
                    upstream_ref = repo.lookup_reference(f"refs/heads/{self.config.upstream_branch}")
                    upstream_id = upstream_ref.target
                except KeyError:
                    upstream_ref = main_repo.lookup_reference(f"refs/heads/{self.config.upstream_branch}")
                    upstream_id = upstream_ref.target

                ahead, behind = main_repo.ahead_behind(local_id, upstream_id)
                ahead_behind = (ahead, behind)
            except (KeyError, pygit2.GitError, ValueError):
                ahead_behind = (0, 0)

        return commit_info, ahead_behind, branch_name
