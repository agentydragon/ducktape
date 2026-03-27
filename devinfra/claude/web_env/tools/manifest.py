"""Shared models and I/O for filesystem manifest capture and diff.

NDJSON format: one JSON object per line per filesystem entry.
"""

from __future__ import annotations

import fnmatch
import sys
from pathlib import Path
from typing import IO

import yaml
from pydantic import BaseModel

from util.bazel.runfiles import get_required_path


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
    # Session start hook artifacts: created by devinfra/claude at runtime,
    # not part of the base container image. Treated as expected_only_in_live.
    session_hook_artifacts: list[str] = []
    # Skip owner/group comparison entirely (gVisor user namespaces make
    # ownership info in the live capture meaningless — all UIDs map to one user).
    ignore_owner: bool = False
    ignore_group: bool = False
    ignore_perms: bool = False

    def matching_skip(self, path: str) -> str | None:
        for p in self.skip_paths:
            if path == p or path.startswith(p + "/"):
                return p
        return None

    def should_skip(self, path: str) -> bool:
        return self.matching_skip(path) is not None

    def matching_volatile(self, path: str) -> str | None:
        for pat in self.volatile_paths:
            if fnmatch.fnmatch(path, pat):
                return pat
        return None

    def is_volatile(self, path: str) -> bool:
        return self.matching_volatile(path) is not None

    def matching_hash_ok(self, path: str) -> str | None:
        for pat in self.hash_may_differ:
            if fnmatch.fnmatch(path, pat):
                return pat
        return None

    def hash_ok_to_differ(self, path: str) -> bool:
        return self.matching_hash_ok(path) is not None

    def matching_only_in_live(self, path: str) -> tuple[str, str] | None:
        """Return (category, pattern) for matching only_in_live or session_hook_artifacts."""
        for pat in self.only_in_live:
            if fnmatch.fnmatch(path, pat):
                return ("only_in_live", pat)
        for pat in self.session_hook_artifacts:
            if fnmatch.fnmatch(path, pat):
                return ("session_hook_artifacts", pat)
        return None

    def expected_only_in_live(self, path: str) -> bool:
        return self.matching_only_in_live(path) is not None

    def matching_only_in_built(self, path: str) -> str | None:
        for pat in self.only_in_built:
            if fnmatch.fnmatch(path, pat):
                return pat
        return None

    def expected_only_in_built(self, path: str) -> bool:
        return self.matching_only_in_built(path) is not None

    def all_patterns(self) -> list[tuple[str, str]]:
        """All (category, pattern) pairs across all exclusion categories."""
        result: list[tuple[str, str]] = [("skip_paths", p) for p in self.skip_paths]
        result.extend(("volatile_paths", p) for p in self.volatile_paths)
        result.extend(("hash_may_differ", p) for p in self.hash_may_differ)
        result.extend(("only_in_live", p) for p in self.only_in_live)
        result.extend(("session_hook_artifacts", p) for p in self.session_hook_artifacts)
        result.extend(("only_in_built", p) for p in self.only_in_built)
        return result


def load_exclusions(path: str | None) -> Exclusions:
    if not path:
        return Exclusions()
    text = Path(path).read_text()
    if path.endswith((".yaml", ".yml")):
        data = yaml.safe_load(text)
        return Exclusions.model_validate(data)
    return Exclusions.model_validate_json(text)


def load_default_exclusions() -> Exclusions:
    """Load the bundled exclusions.yaml from runfiles."""
    path = get_required_path("_main/devinfra/claude/web_env/exclusions.yaml")
    data = yaml.safe_load(path.read_text())
    return Exclusions.model_validate(data)


def parse_ndjson(path: str | Path) -> dict[str, Entry]:
    """Parse an NDJSON manifest file into a dict keyed by path."""
    entries: dict[str, Entry] = {}
    with Path(path).open() as fh:
        for raw_line in fh:
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            entry = Entry.model_validate_json(stripped)
            entries[entry.path] = entry
    return entries


def write_entry(entry: Entry, out: IO[str] = sys.stdout) -> None:
    """Write a single Entry as an NDJSON line."""
    out.write(entry.to_ndjson_line() + "\n")
