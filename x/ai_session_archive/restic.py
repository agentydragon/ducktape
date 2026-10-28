"""Supported Restic JSON and extraction interfaces."""

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import BinaryIO


class ResticOutputError(ValueError):
    pass


class ResticCommandError(RuntimeError):
    def __init__(self, arguments: tuple[str, ...], returncode: int, stderr: bytes):
        self.arguments = arguments
        self.returncode = returncode
        self.stderr = stderr.decode("utf-8", errors="replace").strip()
        super().__init__(f"restic {arguments[0]} failed with exit code {returncode}: {self.stderr}")


@dataclass(frozen=True)
class ResticSnapshot:
    id: str
    captured_at: datetime
    parent_id: str | None
    tree_id: str | None
    paths: tuple[str, ...]


@dataclass(frozen=True)
class ResticFile:
    path: str
    file_type: str
    byte_size: int
    modified_at: datetime | None


@dataclass(frozen=True)
class ResticChange:
    path: str
    modifier: str


class ResticRepository:
    def __init__(self, *, binary: str, environment: Mapping[str, str]):
        self.binary = binary
        self.environment = dict(environment)

    def _run(self, arguments: tuple[str, ...]) -> bytes:
        result = subprocess.run([self.binary, *arguments], env=self.environment, capture_output=True, check=False)
        if result.returncode != 0:
            raise ResticCommandError(arguments, result.returncode, result.stderr)
        return result.stdout

    def list_snapshots(self) -> list[ResticSnapshot]:
        try:
            payload = json.loads(self._run(("snapshots", "--json")))
        except json.JSONDecodeError as exc:
            raise ResticOutputError("restic snapshots returned invalid JSON") from exc
        if not isinstance(payload, list):
            raise ResticOutputError("restic snapshots JSON was not an array")
        snapshots = []
        for item in payload:
            if not isinstance(item, dict):
                raise ResticOutputError("restic snapshots JSON contained a non-object entry")
            try:
                snapshot_id = str(item["id"])
                captured_at = _parse_datetime(item["time"])
                paths = tuple(str(path) for path in item["paths"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ResticOutputError("restic snapshot entry is missing required fields") from exc
            snapshots.append(
                ResticSnapshot(
                    id=snapshot_id,
                    captured_at=captured_at,
                    parent_id=str(item["parent"]) if item.get("parent") else None,
                    tree_id=str(item["tree"]) if item.get("tree") else None,
                    paths=paths,
                )
            )
        return sorted(snapshots, key=lambda row: (row.captured_at, row.id))

    def list_files(self, snapshot_id: str, path: str | None = None) -> list[ResticFile]:
        arguments = ("ls", "--json", snapshot_id) if path is None else ("ls", "--json", snapshot_id, path)
        output = self._run(arguments)
        files = []
        for line_number, raw_line in enumerate(output.splitlines(), start=1):
            if not raw_line:
                continue
            try:
                item = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ResticOutputError(f"restic ls returned invalid JSON on line {line_number}") from exc
            if not isinstance(item, dict):
                raise ResticOutputError(f"restic ls returned a non-object on line {line_number}")
            message_type = item.get("message_type", item.get("struct_type"))
            if message_type == "snapshot":
                continue
            if message_type != "node":
                raise ResticOutputError(
                    f"restic ls returned unknown message type on line {line_number}: {message_type!r}"
                )
            try:
                path = str(item["path"])
                size = int(item.get("size", 0))
                file_type = str(item["type"])
                modified_at = _parse_datetime(item["mtime"]) if item.get("mtime") else None
            except (KeyError, TypeError, ValueError) as exc:
                raise ResticOutputError(f"restic ls node is missing required fields on line {line_number}") from exc
            if size < 0:
                raise ResticOutputError(f"restic ls returned a negative file size on line {line_number}")
            files.append(
                ResticFile(path=str(PurePosixPath(path)), file_type=file_type, byte_size=size, modified_at=modified_at)
            )
        return files

    def diff_snapshots(self, before_snapshot_id: str, after_snapshot_id: str) -> list[ResticChange]:
        output = self._run(("diff", "--json", before_snapshot_id, after_snapshot_id))
        changes = []
        for line_number, raw_line in enumerate(output.splitlines(), start=1):
            if not raw_line:
                continue
            try:
                item = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ResticOutputError(f"restic diff returned invalid JSON on line {line_number}") from exc
            if not isinstance(item, dict):
                raise ResticOutputError(f"restic diff returned a non-object on line {line_number}")
            message_type = item.get("message_type")
            if message_type == "statistics":
                continue
            if message_type != "change":
                raise ResticOutputError(
                    f"restic diff returned unknown message type on line {line_number}: {message_type!r}"
                )
            try:
                changes.append(ResticChange(path=str(PurePosixPath(item["path"])), modifier=str(item["modifier"])))
            except (KeyError, TypeError, ValueError) as exc:
                raise ResticOutputError(f"restic diff change is missing required fields on line {line_number}") from exc
        return changes

    def dump_to(self, snapshot_id: str, path: str, destination: BinaryIO) -> None:
        arguments = ("dump", snapshot_id, "--", path)
        result = subprocess.run(
            [self.binary, *arguments], env=self.environment, stdout=destination, stderr=subprocess.PIPE, check=False
        )
        if result.returncode != 0:
            raise ResticCommandError(arguments, result.returncode, result.stderr)


def _parse_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid RFC3339 timestamp: {value!r}") from exc
