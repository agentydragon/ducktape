"""Shell helpers for emitting 'cd' commands."""

import shlex
from pathlib import Path

from .shell_utils import emit_command


def emit_cd_command(dest_repo: Path, *, main_repo: Path) -> None:
    """Emit a cd command for shell execution.

    - For main repo targets: always cd to the repo root
    - For worktree targets: best-effort relative preservation within the destination
    """
    cwd = Path.cwd()
    if dest_repo == main_repo:
        dest_path = dest_repo
    else:
        try:
            rel = cwd.relative_to(dest_repo)
            dest_path = dest_repo / rel
        except ValueError:
            dest_path = dest_repo
    emit_command(f"cd {shlex.quote(str(dest_path))}")
