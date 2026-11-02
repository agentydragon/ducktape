"""Concurrent-safe writer for framework-neutral test output records."""

from __future__ import annotations

import fcntl
import os
import stat
import tempfile
from pathlib import Path

from util.testing.output_records import (
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


def _write_temporary_file(directory: Path, prefix: str, data: bytes) -> Path:
    fd, temporary_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    temporary_path = Path(temporary_name)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    return temporary_path


def _publish_record(record: OutputRecord, record_path: Path, records_dir: Path) -> None:
    exclude = set()
    if record.label is None:
        exclude.add("label")
    if record.title is None:
        exclude.add("title")
    serialized = record.model_dump_json(by_alias=True, exclude=exclude).encode("utf-8")
    temporary_path = _write_temporary_file(records_dir, f".{record_path.stem}.", serialized)
    try:
        # Hard-linking exposes the complete JSON file and refuses to replace an existing claim.
        os.link(temporary_path, record_path)
        _fsync_directory(records_dir)
    finally:
        temporary_path.unlink(missing_ok=True)


def publish_output(
    output_dir: Path,
    *,
    case_id: str,
    checkpoint: str | None,
    role: str,
    data: bytes,
    suffix: str,
    label: str | None = None,
    title: str | None = None,
) -> Path:
    """Write a payload, then atomically publish its record; return the payload path."""
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    validate_suffix(suffix)
    key = output_key(case_id, checkpoint, role)

    root = output_dir / TEST_OUTPUTS_DIR
    records_dir = root / RECORDS_DIR
    files_dir = root / FILES_DIR
    locks_dir = root / LOCKS_DIR
    for directory in (root, records_dir, files_dir, locks_dir):
        _ensure_directory(directory)

    payload_path = files_dir / f"{key}.{suffix}"
    record_path = records_dir / f"{key}.json"
    record = OutputRecord.model_validate(
        {
            "schema": SCHEMA,
            "case_id": case_id,
            "checkpoint": checkpoint,
            "role": role,
            "path": f"{FILES_DIR}/{payload_path.name}",
            "label": label,
            "title": title,
        }
    )

    lock_path = locks_dir / f"{key}.lock"
    lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = os.open(lock_path, lock_flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise ValueError(f"test output claim lock must be a regular file: {lock_path}")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if record_path.exists() or record_path.is_symlink():
            raise FileExistsError(f"test output identity already claimed: {record_path}")

        temporary_payload = _write_temporary_file(files_dir, f".{key}.", data)
        payload_installed = False
        try:
            # The record is not visible until this complete payload is in place.
            os.link(temporary_payload, payload_path)
            payload_installed = True
            _fsync_directory(files_dir)
            _publish_record(record, record_path, records_dir)
        except BaseException:
            if payload_installed and not (record_path.exists() or record_path.is_symlink()):
                payload_path.unlink(missing_ok=True)
                _fsync_directory(files_dir)
            raise
        finally:
            temporary_payload.unlink(missing_ok=True)
    finally:
        os.close(lock_fd)

    return payload_path
