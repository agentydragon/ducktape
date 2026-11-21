"""Map archived JSONL paths to harness session identities."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

from ai_session_archive.models import Harness


@dataclass(frozen=True)
class HarnessRoot:
    """A configured harness root as it appears inside Restic snapshots."""

    harness: Harness
    path: PurePosixPath

    def __init__(self, harness: Harness, path: str | PurePosixPath):
        normalized = PurePosixPath(path)
        if str(normalized) in ("", "."):
            raise ValueError("harness root must be an explicit non-empty path")
        object.__setattr__(self, "harness", harness)
        object.__setattr__(self, "path", normalized)


@dataclass(frozen=True)
class SourceLocation:
    harness: Harness
    relative_path: PurePosixPath
    source_path: str
    native_session_id: str
    parent_source_path: str | None


def classify_source_path(path: str, roots: Iterable[HarnessRoot]) -> SourceLocation | None:
    archived_path = PurePosixPath(path)
    archived_parts = archived_path.parts
    for root in roots:
        root_parts = root.path.parts
        if archived_parts[: len(root_parts)] == root_parts:
            relative_parts = archived_parts[len(root_parts) :]
        elif tuple(part for part in archived_parts if part != "/")[: len(root_parts)] == tuple(
            part for part in root_parts if part != "/"
        ):
            stripped = tuple(part for part in archived_parts if part != "/")
            relative_parts = stripped[len(tuple(part for part in root_parts if part != "/")) :]
        else:
            continue
        relative = PurePosixPath(*relative_parts)
        if not relative_parts or relative.suffix != ".jsonl":
            return None
        if root.harness == Harness.CLAUDE:
            supported = len(relative_parts) >= 3 and relative_parts[0] == "projects"
        else:
            supported = len(relative_parts) >= 2 and relative_parts[0] in ("sessions", "archived_sessions")
        if not supported:
            return None
        parent_source_path = None
        if root.harness == Harness.CLAUDE and "subagents" in relative_parts:
            index = len(relative_parts) - 1 - tuple(reversed(relative_parts)).index("subagents")
            parent_parts = list(relative_parts[:index])
            if parent_parts:
                parent_parts[-1] = f"{parent_parts[-1]}.jsonl"
                parent_relative = PurePosixPath(*parent_parts)
                archive_root_parts = archived_parts[: len(archived_parts) - len(relative_parts)]
                parent_source_path = str(PurePosixPath(*archive_root_parts) / parent_relative)
        return SourceLocation(
            harness=root.harness,
            relative_path=relative,
            source_path=path,
            native_session_id=relative.stem,
            parent_source_path=parent_source_path,
        )
    return None
