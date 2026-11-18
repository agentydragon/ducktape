"""Concurrent-safe writer for framework-neutral test output records."""

from __future__ import annotations

import fcntl
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

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
        """Publish a payload and its record, encoding text as UTF-8. Return the payload path."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        elif not isinstance(data, bytes):
            raise TypeError("data must be bytes or str")
        with self._stage_output(checkpoint=checkpoint, role=role, suffix=suffix, label=label, title=title) as (
            output,
            payload_path,
        ):
            output.write(data)
        return payload_path

    def publish_file(
        self,
        *,
        source: Path,
        role: str,
        checkpoint: str | None = None,
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
            self._stage_output(checkpoint=checkpoint, role=role, suffix=suffix, label=label, title=title) as (
                output,
                payload_path,
            ),
        ):
            shutil.copyfileobj(input_file, output)
        return payload_path

    @contextmanager
    def _stage_output(
        self, *, checkpoint: str | None, role: str, suffix: str, label: str | None, title: str | None
    ) -> Iterator[tuple[IO[bytes], Path]]:
        validate_suffix(suffix)
        key = output_key(self.case_id, checkpoint, role)
        root = self.output_dir / TEST_OUTPUTS_DIR
        records_dir = root / RECORDS_DIR
        files_dir = root / FILES_DIR
        locks_dir = root / LOCKS_DIR
        for directory in (root, records_dir, files_dir, locks_dir):
            _ensure_directory(directory)

        record = OutputRecord(
            schema_=SCHEMA,
            case_id=self.case_id,
            checkpoint=checkpoint,
            role=role,
            path=f"{FILES_DIR}/{key}.{suffix}",
            label=label,
            title=title,
        )
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{key}.", suffix=".tmp", dir=files_dir, delete_on_close=False
        ) as temporary:
            yield temporary.file, root / record.path
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary.close()
            _publish_temporary_payload(Path(temporary.name), root=root, key=key, record=record)
