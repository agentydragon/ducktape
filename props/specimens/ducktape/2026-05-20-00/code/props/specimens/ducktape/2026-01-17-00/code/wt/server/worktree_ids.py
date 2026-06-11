"""Server-only worktree authorization functions.

This module contains functions that should NEVER be imported by client code.
These operations are server-authority-only to maintain the security model.
"""

from pathlib import Path

from ..shared.configuration import Configuration
from ..shared.protocol import WorktreeID


def make_worktree_id(dirname: str) -> WorktreeID:
    """Server-only: Create a worktree ID from directory name under worktrees dir.

    This function should NEVER be called from client code. Clients should obtain
    WorktreeIDs from server responses only.
    """
    return WorktreeID(f"wtid:{dirname}")


def parse_worktree_id(wtid: WorktreeID) -> str:
    """Server-only: Extract directory name from WorktreeID."""
    s = str(wtid)
    if not s.startswith("wtid:"):
        raise ValueError(f"Invalid worktree ID format: {wtid}")
    return s.removeprefix("wtid:")


def wtid_to_path(config: Configuration, wtid: WorktreeID) -> Path:
    """Server-only: Convert WorktreeID to absolute worktree path using configured worktrees_dir."""
    name = parse_worktree_id(wtid)
    return (config.worktrees_dir / name).resolve()
