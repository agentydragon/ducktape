"""Schema and stable identity helpers for test output records."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA: Literal["ducktape.test-output.v1"] = "ducktape.test-output.v1"
TEST_OUTPUTS_DIR = "test-outputs"
RECORDS_DIR = "records"
FILES_DIR = "files"
LOCKS_DIR = ".locks"

_SAFE_SUFFIX = re.compile(r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*\Z")


def _validate_text(value: str, field: str, *, human_readable: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if not value or (human_readable and not value.strip()):
        raise ValueError(f"{field} must not be empty")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8") from exc
    return value


def output_key(case_id: str, checkpoint: str | None, role: str) -> str:
    """Return the deterministic key for one logical output identity."""
    _validate_text(case_id, "case_id")
    if checkpoint is not None:
        _validate_text(checkpoint, "checkpoint")
    _validate_text(role, "role")
    encoded = json.dumps([case_id, checkpoint, role], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_output_path(value: str) -> str:
    """Validate a canonical relative POSIX path beneath ``files/``."""
    if not isinstance(value, str):
        raise TypeError("path must be a string")
    if not value or "\\" in value or "\x00" in value:
        raise ValueError("path must be a safe relative path beneath files/")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or len(path.parts) < 2
        or path.parts[0] != FILES_DIR
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ValueError("path must be a safe relative path beneath files/")
    return value


def validate_suffix(value: str) -> str:
    """Validate a filename suffix without its leading dot."""
    if not isinstance(value, str) or not _SAFE_SUFFIX.fullmatch(value):
        raise ValueError("suffix must contain only filename extension segments")
    return value


class OutputRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    schema_: Literal["ducktape.test-output.v1"] = Field(alias="schema")
    case_id: str
    checkpoint: str | None
    role: str
    path: str
    label: str | None = None
    title: str | None = None

    @field_validator("case_id")
    @classmethod
    def validate_case_id(cls, value: str) -> str:
        return _validate_text(value, "case_id")

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        return _validate_text(value, "role")

    @field_validator("checkpoint")
    @classmethod
    def validate_checkpoint(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_text(value, "checkpoint")
        return value

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_text(value, "label", human_readable=True)
        return value

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_text(value, "title", human_readable=True)
        return value

    @field_validator("path")
    @classmethod
    def validate_record_path(cls, value: str) -> str:
        return validate_output_path(value)
