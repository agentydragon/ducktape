"""Concurrent-safe writer for framework-neutral test output records."""

from __future__ import annotations

import fcntl
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Literal

from atomicwrites import atomic_write

from devinfra.testing.output_records import (
    FILES_DIR,
    LOCKS_DIR,
    RECORDS_DIR,
    SCHEMA,
    TEST_OUTPUTS_DIR,
    OutputRecord,
    output_key,
    validate_suffix,
)


def _ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"test output directory must not be a symlink or file: {path}")


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_record(record: OutputRecord, record_path: Path) -> None:
    # No overwrite. The record becomes visible only after its payload is complete.
    with atomic_write(record_path, encoding="utf-8", overwrite=False) as output:
        # A null checkpoint is required by the schema. Only label and title have defaults.
        output.write(record.model_dump_json(by_alias=True, exclude_defaults=True))


@contextmanager
def _stage_output(
    output_dir: Path,
    *,
    case_id: str,
    checkpoint: str | None,
    role: str,
    suffix: str,
    mode: Literal["w", "wb"],
    label: str | None,
    title: str | None,
) -> Iterator[tuple[IO[Any], Path]]:
    validate_suffix(suffix)
    key = output_key(case_id, checkpoint, role)
    root = output_dir / TEST_OUTPUTS_DIR
    records_dir = root / RECORDS_DIR
    files_dir = root / FILES_DIR
    locks_dir = root / LOCKS_DIR
    for directory in (root, records_dir, files_dir, locks_dir):
        _ensure_directory(directory)

    record = OutputRecord(
        schema_=SCHEMA,
        case_id=case_id,
        checkpoint=checkpoint,
        role=role,
        path=f"{FILES_DIR}/{key}.{suffix}",
        label=label,
        title=title,
    )
    with tempfile.NamedTemporaryFile(
        mode=mode,
        encoding="utf-8" if mode == "w" else None,
        prefix=f".{key}.",
        suffix=".tmp",
        dir=files_dir,
        delete_on_close=False,
    ) as temporary:
        yield temporary.file, root / record.path
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary.close()
        _publish_temporary_payload(Path(temporary.name), root=root, key=key, record=record)


def _publish_temporary_payload(temporary_payload: Path, *, root: Path, key: str, record: OutputRecord) -> None:
    payload_path = root / record.path
    record_path = root / RECORDS_DIR / f"{key}.json"
    lock_path = root / LOCKS_DIR / f"{key}.lock"
    # One record identity can name different payload suffixes. Serialize the two-file commit.
    with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "rb") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ValueError(f"test output claim lock must be a regular file: {lock_path}")
        fcntl.flock(lock, fcntl.LOCK_EX)
        if record_path.exists() or record_path.is_symlink():
            raise FileExistsError(f"test output identity already claimed: {record_path}")

        payload_installed = False
        try:
            # The record is not visible until this complete payload is in place.
            os.link(temporary_payload, payload_path)
            payload_installed = True
            _fsync_directory(payload_path.parent)
            _publish_record(record, record_path)
        except BaseException:
            if payload_installed and not (record_path.exists() or record_path.is_symlink()):
                payload_path.unlink(missing_ok=True)
                _fsync_directory(payload_path.parent)
            raise


def publish_output(
    output_dir: Path,
    *,
    case_id: str,
    checkpoint: str | None = None,
    role: str,
    data: bytes | str,
    suffix: str,
    label: str | None = None,
    title: str | None = None,
) -> Path:
    """Write a payload, then atomically publish its record; return the payload path.

    Text payloads are encoded as UTF-8.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    elif not isinstance(data, bytes):
        raise TypeError("data must be bytes or str")
    with _stage_output(
        output_dir,
        case_id=case_id,
        checkpoint=checkpoint,
        role=role,
        suffix=suffix,
        mode="wb",
        label=label,
        title=title,
    ) as (output, payload_path):
        output.write(data)
    return payload_path


@contextmanager
def open_output(
    output_dir: Path,
    *,
    case_id: str,
    checkpoint: str | None = None,
    role: str,
    suffix: str,
    mode: Literal["w", "wb"] = "w",
    label: str | None = None,
    title: str | None = None,
) -> Iterator[IO[Any]]:
    """Accept file-style writes and publish only after a successful context exit.

    The handle writes to a temporary file; use publish_file for existing files.
    """
    if mode not in ("w", "wb"):
        raise ValueError("mode must be 'w' or 'wb'")
    with _stage_output(
        output_dir,
        case_id=case_id,
        checkpoint=checkpoint,
        role=role,
        suffix=suffix,
        mode=mode,
        label=label,
        title=title,
    ) as (output, _):
        yield output


def publish_file(
    output_dir: Path,
    *,
    case_id: str,
    checkpoint: str | None = None,
    role: str,
    source: Path,
    label: str | None = None,
    title: str | None = None,
) -> Path:
    """Publish a source file while preserving its filename extension(s)."""
    suffixes = source.suffixes
    if not suffixes:
        raise ValueError(f"source file must have a filename suffix: {source}")
    suffix = ".".join(suffix.lstrip(".") for suffix in suffixes)
    with (
        source.open("rb") as input_file,
        _stage_output(
            output_dir,
            case_id=case_id,
            checkpoint=checkpoint,
            role=role,
            suffix=suffix,
            mode="wb",
            label=label,
            title=title,
        ) as (output, payload_path),
    ):
        shutil.copyfileobj(input_file, output)
    return payload_path


@dataclass(frozen=True)
class BoundTestOutputs:
    """Publish roles and checkpoints under one framework-neutral case identity."""

    output_dir: Path
    case_id: str

    def publish(
        self,
        *,
        checkpoint: str | None = None,
        role: str,
        data: bytes | str,
        suffix: str,
        label: str | None = None,
        title: str | None = None,
    ) -> Path:
        return publish_output(
            self.output_dir,
            case_id=self.case_id,
            checkpoint=checkpoint,
            role=role,
            data=data,
            suffix=suffix,
            label=label,
            title=title,
        )

    def open(
        self,
        *,
        role: str,
        suffix: str,
        checkpoint: str | None = None,
        mode: Literal["w", "wb"] = "w",
        label: str | None = None,
        title: str | None = None,
    ) -> AbstractContextManager[IO[Any]]:
        """Return a file-style handle that publishes after successful close."""
        return open_output(
            self.output_dir,
            case_id=self.case_id,
            checkpoint=checkpoint,
            role=role,
            suffix=suffix,
            mode=mode,
            label=label,
            title=title,
        )

    def publish_file(
        self,
        *,
        source: Path,
        role: str,
        checkpoint: str | None = None,
        label: str | None = None,
        title: str | None = None,
    ) -> Path:
        return publish_file(
            self.output_dir,
            case_id=self.case_id,
            checkpoint=checkpoint,
            role=role,
            source=source,
            label=label,
            title=title,
        )
