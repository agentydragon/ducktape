"""Block changes to code/ in committed specimen snapshots.

A snapshot is "committed" if its issues/ directory exists in HEAD.
Once committed, the code/ directory is immutable.
"""

from pathlib import PurePosixPath

import pygit2
from pygit2.enums import DeltaStatus

_SPECIMENS_PREFIX = "props/specimens/"


def check_specimen_code_changes(deltas: list[pygit2.DiffDelta], head_tree: pygit2.Tree | None) -> list[str]:
    """Return paths of staged specimen code/ files in committed snapshots."""
    if head_tree is None:
        return []

    violations: list[str] = []
    for d in deltas:
        if d.status == DeltaStatus.DELETED:
            continue
        path = d.new_file.path
        if not path.startswith(_SPECIMENS_PREFIX):
            continue
        parts = PurePosixPath(path).parts
        if "code" not in parts:
            continue
        code_idx = parts.index("code")
        if code_idx < 2:
            continue
        snapshot_dir = "/".join(parts[:code_idx])
        try:
            head_tree[f"{snapshot_dir}/issues"]
        except KeyError:
            continue
        violations.append(path)

    return violations
