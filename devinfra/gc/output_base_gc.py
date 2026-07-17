"""Find and safely remove Bazel output bases whose workspaces are gone.

Only default, MD5-named output bases directly below one output user root are
eligible. The workspace is recovered from whichever of the base's own records
(server/cmdline, README, DO_NOT_BUILD_HERE) are present — a dead or never-started
server does not block cleanup — and every present record must agree and hash to the
base name. Ambiguous state is reported for manual review and is never deleted.
"""

import errno
import fcntl
import hashlib
import os
import pwd
import re
import shutil
import stat
import subprocess
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


def _optional_owned_regular(path: Path, *, uid: int, limit: int = _METADATA_LIMIT) -> bytes | None:
    """Contents of a uid-owned regular file, or None if it is absent.

    Only a missing file is tolerated: a present-but-unreadable, non-regular,
    wrong-owner, or oversized file still raises, so corrupt provenance never passes
    silently as "not present".
    """
    try:
        contents, _ = _read_owned_regular(path, uid=uid, limit=limit)
    except MetadataError as error:
        if isinstance(error.__cause__, FileNotFoundError):
            return None
        raise
    return contents


def _workspace_from_cmdline(base: Path, *, uid: int) -> Path | None:
    contents = _optional_owned_regular(base / "server" / "cmdline", uid=uid)
    if contents is None:
        return None
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
    return Path(_single_flag(arguments, "--workspace_directory"))


def _workspace_from_readme(base: Path, *, uid: int) -> Path | None:
    contents = _optional_owned_regular(base / "README", uid=uid)
    if contents is None:
        return None
    first_line = contents.splitlines()[0] if contents else b""
    prefix = b"WORKSPACE: "
    if not first_line.startswith(prefix):
        raise MetadataError("README first line is not 'WORKSPACE: <path>'")
    return Path(os.fsdecode(first_line.removeprefix(prefix)))


def _workspace_from_marker(base: Path, *, uid: int) -> Path | None:
    contents = _optional_owned_regular(base / "DO_NOT_BUILD_HERE", uid=uid)
    if contents is None:
        return None
    return Path(os.fsdecode(contents))


def _read_workspace_provenance(base: Path, *, uid: int) -> Path:
    """Recover the base's workspace from any of its independent on-disk records.

    server/cmdline, README, and DO_NOT_BUILD_HERE each record the workspace path,
    and the base directory name is that path's MD5 — the anchor that makes any one
    record authoritative. A dead or never-started server (no server/cmdline) does
    not block classification: every record that *is* present must agree and hash to
    the base name, and only all three being absent is genuinely undetermined.
    """
    sources = {
        "server/cmdline": _workspace_from_cmdline(base, uid=uid),
        "README": _workspace_from_readme(base, uid=uid),
        "DO_NOT_BUILD_HERE": _workspace_from_marker(base, uid=uid),
    }
    present = {name: workspace for name, workspace in sources.items() if workspace is not None}
    if not present:
        raise MetadataError("no workspace record (server/cmdline, README, DO_NOT_BUILD_HERE all absent)")
    if len(set(present.values())) != 1:
        raise MetadataError(f"workspace records disagree: {present}")
    workspace = next(iter(present.values()))
    if not workspace.is_absolute():
        raise MetadataError(f"workspace is not absolute: {workspace}")
    workspace_hash = hashlib.md5(os.fsencode(workspace), usedforsecurity=False).hexdigest()
    if workspace_hash != base.name:
        raise MetadataError(f"workspace hashes to {workspace_hash}, not {base.name}")
    return workspace


def _last_activity_ns(base: Path) -> int:
    timestamps = [base.lstat().st_mtime_ns]
    paths = [
        base / "command.log",
        base / "README",
        base / "DO_NOT_BUILD_HERE",
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
        workspace = _read_workspace_provenance(base, uid=uid)
        last_activity_ns = _last_activity_ns(base)
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
