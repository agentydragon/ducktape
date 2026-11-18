"""Publish framework-neutral test output records and their payloads."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from devinfra.testing.output_records import (
    FILES_DIR,
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


def _publish_temporary_payload(temporary_payload: Path, *, root: Path, key: str, record: OutputRecord) -> None:
    payload_path = root / record.path
    record_path = root / RECORDS_DIR / f"{key}.json"
    # Stage outside records/ so aborted writes are invisible to the collector.
    with tempfile.NamedTemporaryFile(mode="wb", dir=root, delete_on_close=False) as temporary_record:
        # A null checkpoint is required by the schema. Only label and title have defaults.
        temporary_record.write(record.model_dump_json(by_alias=True, exclude_defaults=True).encode("utf-8"))
        temporary_record.close()

        # Both links reject existing paths. The record link decides the winner
        # when two writers choose different payload suffixes for one identity.
        os.link(temporary_payload, payload_path)
        try:
            os.link(temporary_record.name, record_path)
        except OSError:
            payload_path.unlink(missing_ok=True)
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
        for directory in (root, records_dir, files_dir):
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
            temporary.close()
            try:
                _publish_temporary_payload(Path(temporary.name), root=root, key=key, record=record)
            except FileExistsError as error:
                identity = (self.case_id, checkpoint, role)
                raise FileExistsError(f"test output identity already claimed: {identity!r}") from error
