"""Find and safely remove Bazel output bases whose workspaces are gone.

Only default, MD5-named output bases directly below one output user root are
eligible. Bazel's persisted server command line is the primary provenance;
README and DO_NOT_BUILD_HERE are required corroboration rather than APIs.
Ambiguous state is reported for manual review and is never deleted.
"""

import argparse
import errno
import fcntl
import hashlib
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import humanize
from tabulate import tabulate

_HASHED_BASE_RE = re.compile(r"[0-9a-f]{32}")
_QUARANTINE_PREFIX = ".bazel-output-base-gc-"
_METADATA_LIMIT = 4 * 1024 * 1024


class MetadataError(ValueError):
    """Output-base state that is unsafe to interpret automatically."""


@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class PrunableBase:
    path: Path
    workspace: Path
    identity: FileIdentity
    last_activity_ns: int


@dataclass(frozen=True, slots=True)
class RetainedBase:
    path: Path
    workspace: Path | None
    reason: str
    last_activity_ns: int


@dataclass(frozen=True, slots=True)
class ReviewBase:
    path: Path
    reason: str
    last_activity_ns: int | None


type Inspection = PrunableBase | RetainedBase | ReviewBase


@dataclass(frozen=True, slots=True)
class DeletedBase:
    path: Path


@dataclass(frozen=True, slots=True)
class SkippedBase:
    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class FailedBase:
    path: Path
    quarantine: Path | None
    error: str


type DeletionResult = DeletedBase | SkippedBase | FailedBase


def default_output_user_root() -> Path:
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache_home / "bazel" / f"_bazel_{pwd.getpwuid(os.getuid()).pw_name}"


def _identity(metadata: os.stat_result) -> FileIdentity:
    return FileIdentity(device=metadata.st_dev, inode=metadata.st_ino)


def _read_owned_regular(path: Path, *, uid: int, limit: int = _METADATA_LIMIT) -> tuple[bytes, os.stat_result]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise MetadataError(f"cannot open {path.name}: {error.strerror}") from error

    with os.fdopen(fd, "rb") as file:
        metadata = os.fstat(file.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise MetadataError(f"{path.name} is not a regular file")
        if metadata.st_uid != uid:
            raise MetadataError(f"{path.name} is owned by uid {metadata.st_uid}, expected {uid}")
        contents = file.read(limit + 1)
    if len(contents) > limit:
        raise MetadataError(f"{path.name} exceeds {limit} bytes")
    return contents, metadata


def _single_flag(arguments: Sequence[str], name: str) -> str:
    prefix = f"{name}="
    values = [argument.removeprefix(prefix) for argument in arguments if argument.startswith(prefix)]
    if len(values) != 1 or not values[0]:
        raise MetadataError(f"server/cmdline must contain exactly one {name}= argument")
    return values[0]


def _read_server_provenance(base: Path, *, uid: int) -> tuple[Path, int]:
    contents, cmdline_stat = _read_owned_regular(base / "server" / "cmdline", uid=uid)
    raw_arguments = contents.split(b"\0")
    if raw_arguments and raw_arguments[-1] == b"":
        raw_arguments.pop()
    if not raw_arguments or any(not argument for argument in raw_arguments):
        raise MetadataError("server/cmdline is not a non-empty NUL-delimited argument list")
    arguments = [os.fsdecode(argument) for argument in raw_arguments]

    expected_output_base = os.fspath(base.resolve(strict=True))
    output_base = _single_flag(arguments, "--output_base")
    if output_base != expected_output_base:
        raise MetadataError(f"server/cmdline output base is {output_base}, expected {expected_output_base}")

    workspace_text = _single_flag(arguments, "--workspace_directory")
    workspace = Path(workspace_text)
    if not workspace.is_absolute():
        raise MetadataError(f"server/cmdline workspace is not absolute: {workspace_text}")

    readme, readme_stat = _read_owned_regular(base / "README", uid=uid)
    first_line = readme.splitlines()[0] if readme else b""
    expected_readme_line = b"WORKSPACE: " + os.fsencode(workspace)
    if first_line != expected_readme_line:
        raise MetadataError("README workspace does not match server/cmdline")

    marker, marker_stat = _read_owned_regular(base / "DO_NOT_BUILD_HERE", uid=uid)
    if marker != os.fsencode(workspace):
        raise MetadataError("DO_NOT_BUILD_HERE workspace does not match server/cmdline")

    workspace_hash = hashlib.md5(os.fsencode(workspace), usedforsecurity=False).hexdigest()
    if workspace_hash != base.name:
        raise MetadataError(f"workspace hashes to {workspace_hash}, not {base.name}")

    return workspace, max(cmdline_stat.st_mtime_ns, readme_stat.st_mtime_ns, marker_stat.st_mtime_ns)


def _last_activity_ns(base: Path, provenance_mtime_ns: int) -> int:
    timestamps = [base.lstat().st_mtime_ns, provenance_mtime_ns]
    paths = [
        base / "command.log",
        base / "server" / "cmdline",
        base / "server" / "jvm.out",
        base / "server" / "server.starttime",
    ]
    paths.extend(base.glob("command-*.profile.gz"))
    for path in paths:
        try:
            timestamps.append(path.lstat().st_mtime_ns)
        except FileNotFoundError:
            continue
    return max(timestamps)


def _workspace_exists(workspace: Path) -> bool:
    try:
        workspace.lstat()
    except FileNotFoundError:
        return False
    return True


def _read_positive_integer_metadata(path: Path, *, uid: int) -> int:
    contents, _ = _read_owned_regular(path, uid=uid, limit=128)
    try:
        value = int(contents)
    except ValueError as error:
        raise MetadataError(f"{path.name} is not an integer") from error
    if value <= 0:
        raise MetadataError(f"{path.name} must be positive")
    return value


def _server_is_live(base: Path, *, uid: int, proc_root: Path) -> bool:
    """Conservatively treat any extant process with Bazel's recorded PID as live."""
    pid_path = base / "server" / "server.pid.txt"
    try:
        pid_path.lstat()
    except FileNotFoundError:
        return False

    pid = _read_positive_integer_metadata(pid_path, uid=uid)
    try:
        (proc_root / str(pid)).lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise MetadataError(f"cannot verify server pid {pid}: {error.strerror}") from error
    return True


def _unescape_mount_path(value: str) -> Path:
    for escaped, plain in ((r"\040", " "), (r"\011", "\t"), (r"\012", "\n"), (r"\134", "\\")):
        value = value.replace(escaped, plain)
    return Path(value)


def mount_points(*, mountinfo_path: Path = Path("/proc/self/mountinfo")) -> set[Path]:
    try:
        lines = mountinfo_path.read_text().splitlines()
    except OSError as error:
        raise RuntimeError(f"cannot inspect {mountinfo_path}: {error.strerror}") from error
    points: set[Path] = set()
    for line in lines:
        fields = line.split()
        if len(fields) < 6:
            raise RuntimeError(f"malformed mountinfo line: {line}")
        points.add(_unescape_mount_path(fields[4]))
    return points


def _nested_mount(base: Path, points: set[Path]) -> Path | None:
    return next((point for point in sorted(points) if point == base or point.is_relative_to(base)), None)


def inspect_output_base(base: Path, *, uid: int, points: set[Path], proc_root: Path = Path("/proc")) -> Inspection:
    try:
        metadata = base.lstat()
    except OSError as error:
        return ReviewBase(path=base, reason=f"cannot stat output base: {error.strerror}", last_activity_ns=None)
    if not stat.S_ISDIR(metadata.st_mode):
        return ReviewBase(
            path=base, reason="output base is not a real directory", last_activity_ns=metadata.st_mtime_ns
        )
    if metadata.st_uid != uid:
        return ReviewBase(
            path=base,
            reason=f"output base is owned by uid {metadata.st_uid}, expected {uid}",
            last_activity_ns=metadata.st_mtime_ns,
        )

    try:
        workspace, provenance_mtime_ns = _read_server_provenance(base, uid=uid)
        last_activity_ns = _last_activity_ns(base, provenance_mtime_ns)
        lock_stat = (base / "lock").lstat()
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != uid:
            raise MetadataError("lock is not a regular file owned by the current user")
        if _server_is_live(base, uid=uid, proc_root=proc_root):
            return RetainedBase(base, workspace, "Bazel server is live", last_activity_ns)
    except (MetadataError, OSError) as error:
        return ReviewBase(path=base, reason=str(error), last_activity_ns=metadata.st_mtime_ns)

    resolved_base = base.resolve(strict=True)
    nested_mount = _nested_mount(resolved_base, points)
    if nested_mount is not None:
        return ReviewBase(base, f"contains mount point {nested_mount}", last_activity_ns)

    try:
        workspace_exists = _workspace_exists(workspace)
    except OSError as error:
        return ReviewBase(base, f"cannot inspect workspace: {error.strerror}", last_activity_ns)
    if workspace_exists:
        try:
            resolved_workspace = workspace.resolve(strict=True)
        except OSError as error:
            return ReviewBase(base, f"workspace path exists but cannot be resolved: {error.strerror}", last_activity_ns)
        if resolved_workspace != workspace:
            return ReviewBase(base, f"workspace resolves to {resolved_workspace}, not {workspace}", last_activity_ns)
        return RetainedBase(base, workspace, "workspace exists", last_activity_ns)

    return PrunableBase(base, workspace, _identity(metadata), last_activity_ns)


def scan_output_user_root(
    root: Path,
    *,
    uid: int | None = None,
    proc_root: Path = Path("/proc"),
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
) -> list[Inspection]:
    uid = os.getuid() if uid is None else uid
    root = root.resolve(strict=True)
    points = mount_points(mountinfo_path=mountinfo_path)
    inspections: list[Inspection] = []
    for base in sorted(root.iterdir()):
        if _HASHED_BASE_RE.fullmatch(base.name):
            inspections.append(inspect_output_base(base, uid=uid, points=points, proc_root=proc_root))
        elif base.name.startswith(_QUARANTINE_PREFIX):
            inspections.append(ReviewBase(base, "incomplete previous GC quarantine", base.lstat().st_mtime_ns))
    return inspections


@contextmanager
def _bazel_lock(base: Path, *, uid: int) -> Iterator[int]:
    lock_path = base / "lock"
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise MetadataError(f"cannot open lock: {error.strerror}") from error
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != uid:
            raise MetadataError("lock is not a regular file owned by the current user")
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 0, os.SEEK_SET)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise MetadataError("Bazel lock is busy") from error
            raise
        metadata = os.fstat(fd)
        path_metadata = lock_path.lstat()
        if metadata.st_nlink == 0 or _identity(metadata) != _identity(path_metadata):
            raise MetadataError("lock changed while it was being acquired")
        yield fd
    finally:
        os.close(fd)


def _retry_rmtree_after_permission_error(function: Callable[..., object], path: str, error: BaseException) -> None:
    if not isinstance(error, PermissionError):
        raise error
    if function not in (os.unlink, os.rmdir):
        raise error
    parent = Path(path).parent
    metadata = parent.lstat()
    parent.chmod(stat.S_IMODE(metadata.st_mode) | stat.S_IRWXU)
    function(path)


def delete_prunable_bases(
    candidates: Sequence[PrunableBase],
    *,
    uid: int | None = None,
    proc_root: Path = Path("/proc"),
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
) -> list[DeletionResult]:
    uid = os.getuid() if uid is None else uid
    results: list[DeletionResult] = []
    for candidate in candidates:
        quarantine: Path | None = None
        try:
            with _bazel_lock(candidate.path, uid=uid):
                fresh = inspect_output_base(
                    candidate.path, uid=uid, points=mount_points(mountinfo_path=mountinfo_path), proc_root=proc_root
                )
                if not isinstance(fresh, PrunableBase):
                    results.append(SkippedBase(candidate.path, _inspection_reason(fresh)))
                    continue
                if fresh.identity != candidate.identity or fresh.workspace != candidate.workspace:
                    results.append(SkippedBase(candidate.path, "output-base identity or workspace changed"))
                    continue

                quarantine = candidate.path.parent / f"{_QUARANTINE_PREFIX}{candidate.path.name}-{uuid.uuid4().hex}"
                candidate.path.rename(quarantine)
                (quarantine / "lock").unlink()
            assert quarantine is not None
            shutil.rmtree(quarantine, onexc=_retry_rmtree_after_permission_error)
            results.append(DeletedBase(candidate.path))
        except MetadataError as error:
            results.append(SkippedBase(candidate.path, str(error)))
        except RuntimeError as error:
            if quarantine is None:
                results.append(SkippedBase(candidate.path, str(error)))
            else:
                results.append(FailedBase(candidate.path, quarantine, str(error)))
        except OSError as error:
            results.append(FailedBase(candidate.path, quarantine, str(error)))
    return results


def _inspection_reason(inspection: Inspection) -> str:
    if isinstance(inspection, PrunableBase):
        return "workspace is absent"
    return inspection.reason


def _status(inspection: Inspection) -> str:
    if isinstance(inspection, PrunableBase):
        return "PRUNE"
    if isinstance(inspection, RetainedBase):
        return "KEEP"
    return "REVIEW"


def _detail(inspection: Inspection) -> str:
    if isinstance(inspection, PrunableBase):
        return f"{inspection.workspace} (workspace absent)"
    if isinstance(inspection, RetainedBase):
        prefix = f"{inspection.workspace}: " if inspection.workspace is not None else ""
        return prefix + inspection.reason
    return inspection.reason


def _last_activity(inspection: Inspection) -> str:
    if inspection.last_activity_ns is None:
        return "?"
    return (
        datetime.fromtimestamp(inspection.last_activity_ns / 1_000_000_000, tz=UTC)
        .astimezone()
        .isoformat(timespec="seconds")
    )


def allocated_bytes(path: Path) -> int | None:
    try:
        result = subprocess.run(
            ["du", "-sx", "--block-size=1", "--", os.fspath(path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return int(result.stdout.split(maxsplit=1)[0])
    except (OSError, subprocess.CalledProcessError, ValueError, IndexError):
        return None


def render_report(inspections: Sequence[Inspection], *, include_kept: bool, include_sizes: bool) -> str:
    visible = (
        list(inspections) if include_kept else [item for item in inspections if not isinstance(item, RetainedBase)]
    )
    sizes = {item.path: allocated_bytes(item.path) for item in visible} if include_sizes else {}
    headers = ["STATUS", "LAST ACTIVITY", "BASE", "DETAIL"]
    if include_sizes:
        headers.insert(1, "SIZE")
    rows: list[list[str]] = []
    for item in visible:
        row = [_status(item), _last_activity(item), item.path.name, _detail(item)]
        if include_sizes:
            size = sizes[item.path]
            row.insert(1, humanize.naturalsize(size, binary=True) if size is not None else "?")
        rows.append(row)

    counts = {
        "prunable": sum(isinstance(item, PrunableBase) for item in inspections),
        "kept": sum(isinstance(item, RetainedBase) for item in inspections),
        "review": sum(isinstance(item, ReviewBase) for item in inspections),
    }
    parts = [tabulate(rows, headers=headers, tablefmt="plain", disable_numparse=True)] if rows else []
    parts.append(
        f"Summary: {len(inspections)} bases; {counts['prunable']} prunable, {counts['kept']} kept, "
        f"{counts['review']} review"
    )
    if include_sizes:
        candidate_sizes = [sizes[item.path] for item in inspections if isinstance(item, PrunableBase)]
        available_sizes = [size for size in candidate_sizes if size is not None]
        if len(available_sizes) == len(candidate_sizes):
            parts[-1] += f"; {humanize.naturalsize(sum(available_sizes), binary=True)} prunable"
        else:
            parts[-1] += "; prunable size unavailable"
    return "\n".join(parts)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-user-root",
        type=Path,
        default=default_output_user_root(),
        metavar="PATH",
        help="Bazel output user root (default: %(default)s)",
    )
    parser.add_argument("--all", action="store_true", help="also show retained output bases")
    parser.add_argument("--sizes", action="store_true", help="calculate allocated size with du (potentially slow)")
    parser.add_argument("--delete", action="store_true", help="revalidate and remove prunable output bases")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        inspections = scan_output_user_root(args.output_user_root)
    except (OSError, RuntimeError) as error:
        print(error, file=sys.stderr)
        return 1

    print(render_report(inspections, include_kept=args.all, include_sizes=args.sizes))
    candidates = [item for item in inspections if isinstance(item, PrunableBase)]
    if not args.delete:
        if candidates:
            print("Dry run only; pass --delete to remove the prunable bases.")
        return 0

    free_before = shutil.disk_usage(args.output_user_root).free
    results = delete_prunable_bases(candidates)
    free_space_change = shutil.disk_usage(args.output_user_root).free - free_before
    for result in results:
        if isinstance(result, DeletedBase):
            print(f"DELETED {result.path}")
        elif isinstance(result, SkippedBase):
            print(f"SKIPPED {result.path}: {result.reason}", file=sys.stderr)
        else:
            quarantine = f"; quarantine={result.quarantine}" if result.quarantine is not None else ""
            print(f"FAILED {result.path}: {result.error}{quarantine}", file=sys.stderr)
    deleted = sum(isinstance(result, DeletedBase) for result in results)
    skipped = sum(isinstance(result, SkippedBase) for result in results)
    failed = sum(isinstance(result, FailedBase) for result in results)
    print(
        f"Deletion: {deleted} deleted, {skipped} skipped, {failed} failed; "
        f"filesystem free-space change {humanize.naturalsize(free_space_change, binary=True)}"
    )
    return int(skipped > 0 or failed > 0)


if __name__ == "__main__":
    raise SystemExit(main())
