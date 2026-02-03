"""Shared models and I/O for filesystem manifest capture and diff.

NDJSON format: one JSON object per line per filesystem entry.
"""

from __future__ import annotations

import fnmatch
import json
import sys
from pathlib import Path

from pydantic import BaseModel


class Entry(BaseModel):
    """Single filesystem entry."""

    path: str
    type: str  # f=file, d=dir, l=symlink, p=pipe, s=socket, b=block, c=char
    perms: str  # octal string, e.g. "755"
    owner: str
    group: str
    size: int
    sha256: str | None = None
    link_target: str | None = None

    def to_ndjson_line(self) -> str:
        return self.model_dump_json()


class Exclusions(BaseModel):
    """Narrow exclusion rules applied only at diff time (never at capture)."""

    skip_paths: list[str] = []
    hash_may_differ: list[str] = []
    only_in_live: list[str] = []
    only_in_built: list[str] = []
    # Volatile tool installations: any difference is expected (content, presence,
    # permissions). Covers non-deterministic builds like uv tools, rbenv, nvm.
    volatile_paths: list[str] = []
    # Session start hook artifacts: created by tools/claude_hooks at runtime,
    # not part of the base container image. Treated as expected_only_in_live.
    session_hook_artifacts: list[str] = []
    # Skip owner/group comparison entirely (gVisor user namespaces make
    # ownership info in the live capture meaningless — all UIDs map to one user).
    ignore_owner: bool = False
    ignore_group: bool = False
    ignore_perms: bool = False

    def should_skip(self, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in self.skip_paths)

    def is_volatile(self, path: str) -> bool:
        """True if any difference in this path is expected (non-deterministic tool installs)."""
        return any(fnmatch.fnmatch(path, pat) for pat in self.volatile_paths)

    def hash_ok_to_differ(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, pat) for pat in self.hash_may_differ)

    def expected_only_in_live(self, path: str) -> bool:
        # Includes both only_in_live and session_hook_artifacts (both are expected
        # to exist only in the live container, not in the built image)
        all_live_only = self.only_in_live + self.session_hook_artifacts
        return any(fnmatch.fnmatch(path, pat) for pat in all_live_only)

    def expected_only_in_built(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, pat) for pat in self.only_in_built)


def load_exclusions(path: str | None) -> Exclusions:
    if not path:
        return Exclusions()
    text = Path(path).read_text()
    if path.endswith((".yaml", ".yml")):
        import yaml

        data = yaml.safe_load(text)
        return Exclusions.model_validate(data)
    return Exclusions.model_validate_json(text)


def parse_ndjson(path: str) -> dict[str, Entry]:
    """Parse an NDJSON manifest file into a dict keyed by path."""
    entries: dict[str, Entry] = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("{"):
                entry = Entry.model_validate_json(line)
                entries[entry.path] = entry
            else:
                # Legacy TSV fallback
                parts = line.split("\t", 6)
                if len(parts) != 7:
                    continue
                ftype, perms, owner, group, size_str, hash_or_target, fpath = parts
                sha256 = (
                    hash_or_target
                    if ftype == "f" and hash_or_target not in ("-", "LARGE", "UNREADABLE")
                    else None
                )
                link_target = hash_or_target if ftype == "l" else None
                entries[fpath] = Entry(
                    path=fpath,
                    type=ftype,
                    perms=perms,
                    owner=owner,
                    group=group,
                    size=int(size_str) if size_str.isdigit() else 0,
                    sha256=sha256,
                    link_target=link_target,
                )
    return entries


def write_entry(entry: Entry, out: object = sys.stdout) -> None:
    """Write a single Entry as an NDJSON line."""
    out.write(entry.to_ndjson_line() + "\n")  # type: ignore[union-attr]
