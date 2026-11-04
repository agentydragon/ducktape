"""Concurrent-safe writer for framework-neutral test output records."""

from __future__ import annotations

import fcntl
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Literal

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


def _prepare_output(
    output_dir: Path,
    *,
    case_id: str,
    checkpoint: str | None,
    role: str,
    suffix: str,
    label: str | None,
    title: str | None,
) -> tuple[str, OutputRecord, Path, Path, Path, Path, Path]:
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
    return key, record, payload_path, record_path, files_dir, records_dir, locks_dir


def _publish_temporary_payload(
    temporary_payload: Path,
    *,
    key: str,
    record: OutputRecord,
    payload_path: Path,
    record_path: Path,
    files_dir: Path,
    records_dir: Path,
    locks_dir: Path,
) -> None:
    lock_path = locks_dir / f"{key}.lock"
    lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = os.open(lock_path, lock_flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise ValueError(f"test output claim lock must be a regular file: {lock_path}")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if record_path.exists() or record_path.is_symlink():
            raise FileExistsError(f"test output identity already claimed: {record_path}")

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
        os.close(lock_fd)


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
    key, record, payload_path, record_path, files_dir, records_dir, locks_dir = _prepare_output(
        output_dir, case_id=case_id, checkpoint=checkpoint, role=role, suffix=suffix, label=label, title=title
    )
    temporary_payload = _write_temporary_file(files_dir, f".{key}.", data)
    try:
        _publish_temporary_payload(
            temporary_payload,
            key=key,
            record=record,
            payload_path=payload_path,
            record_path=record_path,
            files_dir=files_dir,
            records_dir=records_dir,
            locks_dir=locks_dir,
        )
    finally:
        temporary_payload.unlink(missing_ok=True)

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
    key, record, payload_path, record_path, files_dir, records_dir, locks_dir = _prepare_output(
        output_dir, case_id=case_id, checkpoint=checkpoint, role=role, suffix=suffix, label=label, title=title
    )
    fd, temporary_name = tempfile.mkstemp(prefix=f".{key}.", suffix=".tmp", dir=files_dir)
    temporary_payload = Path(temporary_name)
    try:
        with os.fdopen(fd, mode, encoding="utf-8" if mode == "w" else None) as output:
            yield output
            output.flush()
            os.fsync(output.fileno())
        _publish_temporary_payload(
            temporary_payload,
            key=key,
            record=record,
            payload_path=payload_path,
            record_path=record_path,
            files_dir=files_dir,
            records_dir=records_dir,
            locks_dir=locks_dir,
        )
    finally:
        temporary_payload.unlink(missing_ok=True)


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
    return publish_output(
        output_dir,
        case_id=case_id,
        checkpoint=checkpoint,
        role=role,
        data=source.read_bytes(),
        suffix=".".join(suffix.lstrip(".") for suffix in suffixes),
        label=label,
        title=title,
    )


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
