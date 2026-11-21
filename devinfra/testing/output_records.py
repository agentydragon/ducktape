"""Schema and stable identity helpers for test output records."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

SCHEMA: Literal["ducktape.test-output.v1"] = "ducktape.test-output.v1"
TEST_OUTPUTS_DIR = "test-outputs"
RECORDS_DIR = "records"
FILES_DIR = "files"

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


def is_canonical_output_path(value: str) -> bool:
    """Check the protocol's relative POSIX path spelling without normalizing it."""
    return "\\" not in value and "\x00" not in value and all(part not in ("", ".", "..") for part in value.split("/"))


def validate_output_path(value: str) -> str:
    """Validate a canonical relative POSIX path beneath ``files/``."""
    if not isinstance(value, str):
        raise TypeError("path must be a string")
    if not value.startswith(f"{FILES_DIR}/") or not is_canonical_output_path(value):
        raise ValueError(f"path must be a safe relative path beneath {FILES_DIR}/")
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

    @field_validator("case_id", "role", "checkpoint", "label", "title")
    @classmethod
    def validate_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        field = info.field_name
        assert field is not None
        return _validate_text(value, field, human_readable=field in ("label", "title"))

    @field_validator("path")
    @classmethod
    def validate_record_path(cls, value: str) -> str:
        return validate_output_path(value)
