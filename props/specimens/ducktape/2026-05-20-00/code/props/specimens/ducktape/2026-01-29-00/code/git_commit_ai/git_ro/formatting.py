from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import IntEnum
from pathlib import Path
from typing import Annotated, TypeVar

import pygit2
from pydantic import BaseModel, Field
from pygit2.enums import DeltaStatus, FileStatus

from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel


def _enum_desc(enum: type[IntEnum], pred: Callable[[str], bool] = lambda _: True) -> str:
    """Generate 'value=NAME, ...' from enum members matching predicate."""
    return ", ".join(f"{m.value}={m.name}" for m in enum if pred(m.name))


# Annotated types with value→name mappings in description
AnnotatedFileStatus = Annotated[FileStatus, Field(description=_enum_desc(FileStatus))]
AnnotatedDeltaStatus = Annotated[DeltaStatus, Field(description=_enum_desc(DeltaStatus))]


# -------------------------- pagination models -------------------------------


class ListSlice(OpenAIStrictModeBaseModel):
    """Pagination controls for list outputs."""

    offset: int = Field(ge=0, description="Start index of the window")
    limit: int = Field(ge=0, description="Max items to return (0 = unbounded)")


class TextSlice(OpenAIStrictModeBaseModel):
    """Pagination controls for text outputs."""

    offset_chars: int = Field(ge=0, description="Start character offset")
    max_chars: int = Field(ge=0, description="Max characters to return (0 = all)")


class TextPage(BaseModel):
    body: str
    total_chars: int
    truncated: bool
    next_offset: int | None = None


class StringListPage(BaseModel):
    items: list[str]
    total_items: int
    truncated: bool
    next_offset: int | None = None


T = TypeVar("T")


def _calc_window(total: int, start: int, size: int) -> tuple[int, int, bool, int | None]:
    """Core window math shared by list/text slicing.

    Returns (start, end, truncated, next_offset)."""
    s = max(0, start)
    e = min(total, s + size) if size and size > 0 else total
    truncated = e < total
    next_offset = e if truncated else None
    return s, e, truncated, next_offset


def apply_list_slice[T](items: Sequence[T], slicer: ListSlice) -> tuple[list[T], bool, int | None, int]:
    total = len(items)
    s, e, truncated, next_offset = _calc_window(total, slicer.offset, slicer.limit)
    return list(items[s:e]), truncated, next_offset, total


def apply_text_slice(body: str, slicer: TextSlice) -> TextPage:
    total = len(body)
    s, e, truncated, next_offset = _calc_window(total, slicer.offset_chars, slicer.max_chars)
    return TextPage(body=body[s:e], total_chars=total, truncated=truncated, next_offset=next_offset)


# -------------------------- status models -----------------------------------


class StatusPage(BaseModel):
    """Git status: path → FileStatus flags (bitmask combining INDEX_* and WT_* flags)."""

    entries: dict[str, AnnotatedFileStatus]
    truncated: bool
    next_offset: int | None = None
    total_entries: int


def build_status_page(entries: dict[str, FileStatus], slicer: ListSlice) -> StatusPage:
    # Apply pagination to dict items
    items = list(entries.items())
    window, truncated, next_offset, total = apply_list_slice(items, slicer)
    return StatusPage(entries=dict(window), truncated=truncated, next_offset=next_offset, total_entries=total)


# -------------------------- diff list (name-status) -------------------------


class ChangedFileItem(BaseModel):
    """Represents a changed file in a diff.

    For renames: old_path contains the source path, path contains the destination path.
    For other changes: only path is set, old_path is None.
    """

    path: Path = Field(description="New/current path (destination for renames)")
    old_path: Path | None = Field(description="Old path (for renames only)")
    status: AnnotatedDeltaStatus


class ChangedFilesPage(BaseModel):
    items: list[ChangedFileItem]
    truncated: bool
    next_offset: int | None = None
    total_items: int


def diff_to_changed_files(diff: pygit2.Diff) -> list[ChangedFileItem]:
    """Convert a pygit2 Diff to a list of ChangedFileItem.

    For renamed files, preserves both old_path (source) and path (destination).
    For other changes, only path is set.
    """
    items: list[ChangedFileItem] = []
    for p in diff:
        if p is None:
            continue
        d = p.delta
        # For renames, preserve both paths
        if d.status == pygit2.GIT_DELTA_RENAMED:
            items.append(ChangedFileItem(path=Path(d.new_file.path), old_path=Path(d.old_file.path), status=d.status))
        else:
            # For other changes, use new_file.path if available, otherwise old_file.path
            path = (d.new_file.path or d.old_file.path) if d.new_file else d.old_file.path
            items.append(ChangedFileItem(path=Path(path), old_path=None, status=d.status))
    return items


def build_changed_files_page(items: list[ChangedFileItem], slicer: ListSlice) -> ChangedFilesPage:
    window, truncated, next_offset, total = apply_list_slice(items, slicer)
    return ChangedFilesPage(items=window, truncated=truncated, next_offset=next_offset, total_items=total)


# -------------------------- diff stat (additions/deletions) -----------------


class StatItem(BaseModel):
    """Represents file statistics (additions/deletions) for a diff.

    For renamed files, path is the new (destination) path.
    """

    path: Path
    old_path: Path | None = Field(description="Old path (for renames only)")
    additions: int
    deletions: int


class DiffStatPage(BaseModel):
    items: list[StatItem]
    truncated: bool
    next_offset: int | None = None
    total_items: int


def _count_patch_lines(patch: pygit2.Patch) -> tuple[int, int]:
    add = 0
    delete = 0
    for h in patch.hunks:
        for ln in h.lines:
            if ln.origin == "+":
                add += 1
            elif ln.origin == "-":
                delete += 1
    return add, delete


def diff_to_file_stats(diff: pygit2.Diff) -> list[StatItem]:
    """Convert a pygit2 Diff to file statistics.

    For renamed files, preserves both old_path (source) and path (destination).
    """
    out: list[StatItem] = []
    for patch in diff:
        if patch is None:
            continue
        delta = patch.delta
        additions, deletions = _count_patch_lines(patch)

        # For renames, preserve both paths
        if delta.status == pygit2.GIT_DELTA_RENAMED:
            out.append(
                StatItem(
                    path=Path(delta.new_file.path),
                    old_path=Path(delta.old_file.path),
                    additions=additions,
                    deletions=deletions,
                )
            )
        else:
            # For other changes, use new_file.path if available, otherwise old_file.path
            path = (delta.new_file.path or delta.old_file.path) if delta.new_file else delta.old_file.path
            out.append(StatItem(path=Path(path), old_path=None, additions=additions, deletions=deletions))
    return out


def build_diff_stat_page(items: list[StatItem], slicer: ListSlice) -> DiffStatPage:
    window, truncated, next_offset, total = apply_list_slice(items, slicer)
    return DiffStatPage(items=window, truncated=truncated, next_offset=next_offset, total_items=total)
