"""Find and safely remove Bazel repo-contents-cache entries that no output base references.

Bazel's `LocalRepoContentsCache` lays the cache out as `<contents>/<predeclared-inputs
hash>/<uuid>/` (one fetched repository) beside `<uuid>.recorded_inputs` (its recorded
inputs; Bazel touches that file on every cache hit, so its mtime is the entry's last use).
An output base uses an entry through the symlink `<base>/external/<repo> ->
<contents>/<hash>/<uuid>`. `_trash/` is where Bazel's own GC parks entries before deleting
them, and every Bazel command holds a shared lock on `gc_lock` for its whole duration (that
GC takes it exclusively).

Bazel's GC is age-based only (`--repo_contents_cache_gc_max_age`) and runs on an idle
server; it never asks whether an entry is referenced. This scanner does: an entry that no
known output base links to is prunable, but only when every known base's
`external/` directory and links were inspectable. Deletion takes `gc_lock` exclusively,
so no command can be fetching into the cache — or linking a base to an entry — while one
goes, and removes an entry the way Bazel's GC does (recorded inputs first, then the
directory into `_trash/`), so an interruption leaves nothing a later Bazel GC won't finish.

Blind spot, shared with Bazel's own GC: a base under another output user root that points
`--repo_contents_cache` at this cache is invisible here, and an entry only it references is
deleted; that base re-fetches the repo (or fails until it does), nothing worse.
"""

import enum
import errno
import fcntl
import logging
import os
import re
import shutil
import stat
import uuid
from collections.abc import Iterable, Iterator, Sequence, Set
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import humanize
from tabulate import tabulate

from devinfra.gc.output_base_gc import (
    FileIdentity,
    Inspection,
    PrunableBase,
    allocated_bytes,
    file_identity,
    list_output_bases,
    mount_points,
    nested_mount,
    retry_rmtree_after_permission_error,
)
from devinfra.gc.removal import Failed, RemovalResult, Removed, Skipped
from devinfra.gc.scan_progress import NULL_PROGRESS, ProgressSink
from devinfra.gc.verdict import Prunable, Retained, Review, verdict_of

logger = logging.getLogger(__name__)

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
RECORDED_INPUTS_SUFFIX = ".recorded_inputs"
# Bazel's own files under the cache root (LocalRepoContentsCache.TRASH_PATH and LOCK_PATH).
_TRASH = "_trash"
_GC_LOCK = "gc_lock"


class RepoCacheBusyError(RuntimeError):
    """A Bazel command holds the cache's shared lock; nothing may be removed now."""


def repo_contents_cache(output_user_root: Path) -> Path:
    """Bazel's default `--repo_contents_cache`: `<--repository_cache>/contents`, where the
    repository cache defaults to `<output user root>/cache/repos/v1`."""
    return output_user_root / "cache" / "repos" / "v1" / "contents"


def recorded_inputs_of(contents_dir: Path) -> Path:
    return contents_dir.with_name(contents_dir.name + RECORDED_INPUTS_SUFFIX)


@dataclass(frozen=True, slots=True)
class CacheEntry:
    contents: Path  # <contents>/<hash>/<uuid>
    identity: FileIdentity
    last_used_ns: int  # mtime of the recorded-inputs file, which Bazel touches on every hit


class RetainedEntryReason(enum.StrEnum):
    REFERENCED = enum.auto()  # by a base that stays (KEEP or REVIEW)
    ONLY_PRUNABLE_REFERRERS = enum.auto()  # every referrer is a prunable base; `all --prune` frees it


class ReviewEntryReason(enum.StrEnum):
    MALFORMED_NAME = enum.auto()
    NOT_A_DIRECTORY = enum.auto()
    WRONG_OWNER = enum.auto()  # `detail` is "uid N, expected M"
    MISSING_RECORDED_INPUTS = enum.auto()
    MISSING_CONTENTS = enum.auto()  # a recorded-inputs file without its directory
    NESTED_MOUNT = enum.auto()  # `detail` is the mount point
    CANNOT_STAT = enum.auto()  # `detail` is the OS error
    CANNOT_LIST = enum.auto()
    INCOMPLETE_REFERENCE_SCAN = enum.auto()


@dataclass(frozen=True, slots=True)
class PrunableEntry(Prunable):
    entry: CacheEntry


@dataclass(frozen=True, slots=True)
class RetainedEntry(Retained):
    entry: CacheEntry
    reason: RetainedEntryReason
    referrers: frozenset[Path]  # output bases whose `external/` links to the entry


@dataclass(frozen=True, slots=True)
class ReviewEntry(Review):
    path: Path
    reason: ReviewEntryReason
    detail: str | None = None


type EntryInspection = PrunableEntry | RetainedEntry | ReviewEntry


@dataclass(frozen=True, slots=True)
class ReferenceScanFailure:
    path: Path
    detail: str


@dataclass(frozen=True, slots=True)
class ReferenceInventory:
    referrers: dict[Path, frozenset[Path]]
    failures: tuple[ReferenceScanFailure, ...]

    @property
    def complete(self) -> bool:
        return not self.failures

    @property
    def failure_detail(self) -> str:
        if not self.failures:
            raise ValueError("complete reference inventory has no failure detail")
        first = self.failures[0]
        other_failures = len(self.failures) - 1
        suffix = f" ({other_failures} other inspection failure(s))" if other_failures else ""
        return f"reference inventory is incomplete at {first.path}: {first.detail}{suffix}"


def entry_path(inspection: EntryInspection) -> Path:
    if isinstance(inspection, ReviewEntry):
        return inspection.path
    return inspection.entry.contents


def _inspect_entry(contents_dir: Path, *, uid: int, points: set[Path]) -> CacheEntry | ReviewEntry:
    try:
        metadata = contents_dir.lstat()
    except OSError as error:
        return ReviewEntry(contents_dir, ReviewEntryReason.CANNOT_STAT, detail=error.strerror)
    if not stat.S_ISDIR(metadata.st_mode):
        return ReviewEntry(contents_dir, ReviewEntryReason.NOT_A_DIRECTORY)
    if metadata.st_uid != uid:
        return ReviewEntry(contents_dir, ReviewEntryReason.WRONG_OWNER, detail=f"uid {metadata.st_uid}, expected {uid}")
    try:
        inputs_metadata = recorded_inputs_of(contents_dir).lstat()
    except FileNotFoundError:
        return ReviewEntry(contents_dir, ReviewEntryReason.MISSING_RECORDED_INPUTS)
    except OSError as error:
        return ReviewEntry(contents_dir, ReviewEntryReason.CANNOT_STAT, detail=error.strerror)
    mount = nested_mount(contents_dir, points)
    if mount is not None:
        return ReviewEntry(contents_dir, ReviewEntryReason.NESTED_MOUNT, detail=os.fspath(mount))
    return CacheEntry(contents_dir, file_identity(metadata), inputs_metadata.st_mtime_ns)


def inventory(contents: Path, *, uid: int, points: set[Path]) -> list[CacheEntry | ReviewEntry]:
    """Every `<hash>/<uuid>` entry under `contents` (already resolved), or what stands where
    one should; Bazel's own `_trash/` and `gc_lock` are structure, not entries."""
    items: list[CacheEntry | ReviewEntry] = []
    for hash_dir in sorted(contents.iterdir()):
        if hash_dir.name in (_TRASH, _GC_LOCK):
            continue
        if not _HASH_RE.fullmatch(hash_dir.name):
            items.append(ReviewEntry(hash_dir, ReviewEntryReason.MALFORMED_NAME))
            continue
        try:
            is_dir = stat.S_ISDIR(hash_dir.lstat().st_mode)
        except OSError as error:
            items.append(ReviewEntry(hash_dir, ReviewEntryReason.CANNOT_STAT, detail=error.strerror))
            continue
        if not is_dir:
            items.append(ReviewEntry(hash_dir, ReviewEntryReason.NOT_A_DIRECTORY))
            continue
        try:
            children = sorted(hash_dir.iterdir())
        except OSError as error:
            items.append(ReviewEntry(hash_dir, ReviewEntryReason.CANNOT_LIST, detail=str(error)))
            continue
        names = {child.name for child in children}
        for child in children:
            if child.name.endswith(RECORDED_INPUTS_SUFFIX):
                if child.name.removesuffix(RECORDED_INPUTS_SUFFIX) not in names:
                    items.append(ReviewEntry(child, ReviewEntryReason.MISSING_CONTENTS))
            elif _UUID_RE.fullmatch(child.name):
                items.append(_inspect_entry(child, uid=uid, points=points))
            else:
                items.append(ReviewEntry(child, ReviewEntryReason.MALFORMED_NAME))
    return items


def references(bases: Iterable[Path], contents: Path) -> ReferenceInventory:
    """Cache entry (its contents dir) -> the output bases whose `external/` links to it.

    Missing `external/` directories, vanished entries, and genuine non-symlink entries are
    harmless. Every other listing, link, or path-resolution failure makes the inventory
    incomplete: a caller must not call an entry unreferenced based on a partial scan. Bazel
    writes targets through `--repo_contents_cache` as configured, which may reach `contents`
    (resolved) through a symlinked prefix, so the target's hash directory is resolved before
    comparing.
    """
    referrers: dict[Path, set[Path]] = {}
    resolved_hash_dirs: dict[Path, Path] = {}
    failures: list[ReferenceScanFailure] = []

    def record_failure(path: Path, detail: str) -> None:
        logger.warning("Cannot inspect %s; repo-cache references are unknown: %s", path, detail)
        failures.append(ReferenceScanFailure(path, detail))

    for base in bases:
        external = base / "external"
        try:
            children = list(external.iterdir())
        except FileNotFoundError as error:
            # A missing path has no links. If it still exists, however, it may be a broken
            # symlink or a directory that disappeared while it was being scanned.
            try:
                external.lstat()
            except FileNotFoundError:
                continue
            except OSError as stat_error:
                record_failure(external, stat_error.strerror or str(stat_error))
            else:
                record_failure(external, error.strerror or str(error))
            continue
        except OSError as error:
            record_failure(external, error.strerror or str(error))
            continue
        for child in children:
            try:
                target = child.readlink()
            except FileNotFoundError:
                continue  # the entry vanished after the directory listing
            except OSError as error:
                if error.errno == errno.EINVAL:
                    continue  # Linux readlink reports EINVAL for an existing non-symlink entry
                record_failure(child, error.strerror or str(error))
                continue
            target = external / target  # a no-op for an absolute target
            hash_dir = resolved_hash_dirs.get(target.parent)
            if hash_dir is None:
                try:
                    hash_dir = target.parent.resolve()
                except OSError as error:
                    record_failure(child, error.strerror or str(error))
                    continue
                resolved_hash_dirs[target.parent] = hash_dir
            if hash_dir.parent == contents:
                referrers.setdefault(hash_dir / target.name, set()).add(base)
    return ReferenceInventory(
        referrers={path: frozenset(bases) for path, bases in referrers.items()}, failures=tuple(failures)
    )


def scan_repo_contents_cache(
    contents: Path,
    bases: Sequence[Inspection],
    *,
    uid: int | None = None,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    progress: ProgressSink = NULL_PROGRESS,
) -> list[EntryInspection]:
    """Classify every entry against `bases`: the joint scan's verdict on every output base
    whose `external/` may link into this cache (the root's and the session-env ones). Entries
    are REVIEW when any base's reference inventory is incomplete."""
    uid = os.getuid() if uid is None else uid
    try:
        contents = contents.resolve(strict=True)
    except FileNotFoundError:
        logger.info("No repo contents cache at %s", contents)
        return []
    points = mount_points(mountinfo_path=mountinfo_path)
    items = inventory(contents, uid=uid, points=points)
    reference_inventory = references((base.path for base in bases), contents)
    prunable_bases = {base.path for base in bases if isinstance(base, PrunableBase)}
    logger.info("Classifying %d repo-cache entries against %d output bases", len(items), len(bases))
    if items:
        progress.start_phase("repo-cache", len(items))
    inspections: list[EntryInspection] = []
    for item in items:
        inspection: EntryInspection
        if isinstance(item, ReviewEntry):
            inspection = item
        elif not reference_inventory.complete:
            inspection = ReviewEntry(
                item.contents, ReviewEntryReason.INCOMPLETE_REFERENCE_SCAN, detail=reference_inventory.failure_detail
            )
        else:
            linked_from = reference_inventory.referrers.get(item.contents, frozenset())
            if not linked_from:
                inspection = PrunableEntry(item)
            elif linked_from <= prunable_bases:
                inspection = RetainedEntry(item, RetainedEntryReason.ONLY_PRUNABLE_REFERRERS, linked_from)
            else:
                inspection = RetainedEntry(item, RetainedEntryReason.REFERENCED, linked_from)
        inspections.append(inspection)
        progress.record("repo-cache", verdict_of(inspection))
    return inspections


def unreferenced_after_deleting(entries: Sequence[EntryInspection], deleted_bases: Set[Path]) -> list[PrunableEntry]:
    """Entries prunable once `deleted_bases` are gone: the already-prunable ones plus the retained
    ones whose every referrer was deleted."""
    return [
        entry if isinstance(entry, PrunableEntry) else PrunableEntry(entry.entry)
        for entry in entries
        if isinstance(entry, PrunableEntry) or (isinstance(entry, RetainedEntry) and entry.referrers <= deleted_bases)
    ]


@contextmanager
def _gc_lock(contents: Path) -> Iterator[None]:
    """Bazel's cache lock, held exclusively: every Bazel command holds it shared while it runs
    (`BazelRepositoryModule` acquires it before and releases it after each command), so
    holding it exclusively means no fetch is in flight and none starts until it is released —
    a command that then finds its entry gone re-fetches. Non-blocking: a busy cache skips
    this round rather than stalling a running build. Created if absent, as Bazel does."""
    fd = os.open(contents / _GC_LOCK, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o644)
    try:
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise RepoCacheBusyError("a Bazel command holds the repo contents cache") from error
            raise
        yield
    finally:
        os.close(fd)


@dataclass(frozen=True, slots=True)
class TrashedEntries:
    results: list[RemovalResult[Path]]  # entries skipped or failed while trashing
    trashed: list[tuple[Path, Path]]  # (entry, its directory in `_trash/`), still to be emptied


def trash_prunable_entries(
    candidates: Sequence[PrunableEntry],
    *,
    contents: Path,
    output_user_root: Path,
    session_bases: Sequence[Path],
    uid: int | None = None,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
) -> TrashedEntries:
    """Move each candidate that is still unreferenced into `_trash/`, under Bazel's exclusive
    cache lock; `empty_trash` removes the moved directories after the lock is released.

    Revalidation runs under the lock against a fresh listing of the bases (the root's plus
    `session_bases`, which the caller globbed moments before) and a fresh link pass over
    them: an entry some base linked to since the scan, an incomplete reference inventory, or
    a replaced directory is skipped. Removal mirrors Bazel's GC — recorded inputs first, so no
    command can pick the
    entry up, then the directory renamed into `_trash/` — and the trash is emptied after the
    lock is released, as Bazel does; an interruption leaves at worst trash Bazel's next GC
    empties.
    """
    uid = os.getuid() if uid is None else uid
    contents = contents.resolve(strict=True)
    results: list[RemovalResult[Path]] = []
    trashed: list[tuple[Path, Path]] = []
    try:
        with _gc_lock(contents):
            base_paths = [base for base, _ in list_output_bases(output_user_root, session_bases=session_bases)]
            reference_inventory = references(base_paths, contents)
            points = mount_points(mountinfo_path=mountinfo_path)
            for index, candidate in enumerate(candidates, start=1):
                path = candidate.entry.contents
                logger.info("Trashing repo-cache entry %d/%d %s", index, len(candidates), path)
                fresh = _inspect_entry(path, uid=uid, points=points)
                if isinstance(fresh, ReviewEntry):
                    results.append(Skipped(path, describe_reason(fresh)))
                    continue
                if fresh.identity != candidate.entry.identity:
                    results.append(Skipped(path, "entry was replaced since the scan"))
                    continue
                if path in reference_inventory.referrers:
                    results.append(
                        Skipped(path, f"now referenced by {len(reference_inventory.referrers[path])} output base(s)")
                    )
                    continue
                if not reference_inventory.complete:
                    results.append(Skipped(path, reference_inventory.failure_detail))
                    continue
                trash = contents / _TRASH / uuid.uuid4().hex
                try:
                    trash.parent.mkdir(exist_ok=True)
                    recorded_inputs_of(path).unlink()
                    path.rename(trash)
                except OSError as error:
                    results.append(Failed(path, str(error)))
                    continue
                trashed.append((path, trash))
    except RepoCacheBusyError as error:
        return TrashedEntries([Skipped(candidate.entry.contents, str(error)) for candidate in candidates], [])
    return TrashedEntries(results, trashed)


def empty_trash(path: Path, trash: Path) -> RemovalResult[Path]:
    """Remove one trashed entry; independent of every other, so callers may run several at once."""
    logger.info("Emptying repo-cache trash %s", trash)
    try:
        shutil.rmtree(trash, onexc=retry_rmtree_after_permission_error)
    except OSError as error:
        if error.errno == errno.ENOENT:
            try:
                trash.lstat()
            except FileNotFoundError:
                # Bazel may have consumed this entry from `_trash` after we released its lock.
                return Removed(path)
            except OSError:
                pass
        return Failed(path, str(error), trash)
    return Removed(path)


def delete_prunable_entries(
    candidates: Sequence[PrunableEntry],
    *,
    contents: Path,
    output_user_root: Path,
    session_bases: Sequence[Path],
    uid: int | None = None,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
) -> list[RemovalResult[Path]]:
    trashed = trash_prunable_entries(
        candidates,
        contents=contents,
        output_user_root=output_user_root,
        session_bases=session_bases,
        uid=uid,
        mountinfo_path=mountinfo_path,
    )
    return [*trashed.results, *(empty_trash(path, trash) for path, trash in trashed.trashed)]


def describe_reason(inspection: RetainedEntry | ReviewEntry) -> str:
    match inspection:
        case RetainedEntry(reason=RetainedEntryReason.REFERENCED, referrers=referrers):
            return f"referenced by {len(referrers)} output base(s)"
        case RetainedEntry(reason=RetainedEntryReason.ONLY_PRUNABLE_REFERRERS, referrers=referrers):
            return f"only prunable bases reference it ({len(referrers)}; `all --prune` frees it)"
        case ReviewEntry(reason=ReviewEntryReason.MALFORMED_NAME):
            return "not a <hash>/<uuid> cache entry"
        case ReviewEntry(reason=ReviewEntryReason.NOT_A_DIRECTORY):
            return "entry is not a real directory"
        case ReviewEntry(reason=ReviewEntryReason.MISSING_RECORDED_INPUTS):
            return "no .recorded_inputs file beside it"
        case ReviewEntry(reason=ReviewEntryReason.MISSING_CONTENTS):
            return ".recorded_inputs file without its contents directory"
        case ReviewEntry(reason=ReviewEntryReason.WRONG_OWNER, detail=str() as detail):
            return f"entry is owned by {detail}"
        case ReviewEntry(reason=ReviewEntryReason.NESTED_MOUNT, detail=str() as detail):
            return f"contains mount point {detail}"
        case ReviewEntry(reason=ReviewEntryReason.CANNOT_STAT, detail=str() as detail):
            return f"cannot stat entry: {detail}"
        case ReviewEntry(reason=ReviewEntryReason.CANNOT_LIST, detail=str() as detail):
            return f"cannot list cache directory: {detail}"
        case ReviewEntry(reason=ReviewEntryReason.INCOMPLETE_REFERENCE_SCAN, detail=str() as detail):
            return detail
        case _:
            raise ValueError(f"{inspection=} lacks the detail its reason cites")


def _detail(inspection: EntryInspection) -> str:
    if isinstance(inspection, PrunableEntry):
        return "no output base references it"
    return describe_reason(inspection)


def _last_used(inspection: EntryInspection) -> str:
    if isinstance(inspection, ReviewEntry):
        return "?"
    return (
        datetime.fromtimestamp(inspection.entry.last_used_ns / 1_000_000_000, tz=UTC)
        .astimezone()
        .isoformat(timespec="seconds")
    )


def render_report(
    inspections: Sequence[EntryInspection],
    *,
    contents: Path,
    include_kept: bool,
    include_sizes: bool,
    include_prunable: bool = False,
) -> str:
    contents = contents.resolve()
    visible = [
        item
        for item in inspections
        if isinstance(item, ReviewEntry) or include_kept or (include_prunable and isinstance(item, PrunableEntry))
    ]
    sizes: dict[Path, int | None] = {}
    if include_sizes:
        measured = [item for item in inspections if include_kept or not isinstance(item, RetainedEntry)]
        for index, item in enumerate(measured, start=1):
            logger.info("Measuring repo-cache entry size %d/%d %s", index, len(measured), entry_path(item))
            sizes[entry_path(item)] = allocated_bytes(entry_path(item))
    headers = ["STATUS", "LAST USED", "ENTRY", "DETAIL"]
    if include_sizes:
        headers.insert(1, "SIZE")
    rows: list[list[str]] = []
    for item in visible:
        path = entry_path(item)
        row = [verdict_of(item), _last_used(item), os.path.relpath(path, contents), _detail(item)]
        if include_sizes:
            size = sizes[path]
            row.insert(1, humanize.naturalsize(size, binary=True) if size is not None else "?")
        rows.append(row)

    counts = {
        "prunable": sum(isinstance(item, PrunableEntry) for item in inspections),
        "kept": sum(isinstance(item, RetainedEntry) for item in inspections),
        "review": sum(isinstance(item, ReviewEntry) for item in inspections),
    }
    parts = [tabulate(rows, headers=headers, tablefmt="plain", disable_numparse=True)] if rows else []
    parts.append(
        f"Summary: {len(inspections)} repo-cache entries; {counts['prunable']} prunable, {counts['kept']} kept, "
        f"{counts['review']} review"
    )
    if include_sizes:
        candidate_sizes = [sizes[entry_path(item)] for item in inspections if isinstance(item, PrunableEntry)]
        available_sizes = [size for size in candidate_sizes if size is not None]
        if len(available_sizes) == len(candidate_sizes):
            parts[-1] += f"; {humanize.naturalsize(sum(available_sizes), binary=True)} prunable"
        else:
            parts[-1] += "; prunable size unavailable"
    return "\n".join(parts)
