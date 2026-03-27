"""Shared model types for props core.

Contains validated types used across multiple modules:
- Rationale: Validated explanation text (10-5000 characters)
- SnapshotRelativePath: Snapshot-relative paths with conditional validation
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

from pydantic import PlainSerializer, StringConstraints, ValidationInfo, WrapValidator

# =============================================================================
# Rationale type
# =============================================================================

Rationale = Annotated[str, StringConstraints(min_length=10, max_length=5000, strip_whitespace=True)]
"""Validated rationale text (10-5000 chars, whitespace stripped).

Uses standard Pydantic constraints for proper JSON Schema export.
"""


# =============================================================================
# Path types
# =============================================================================


class FileType(StrEnum):
    """File type classification for snapshot paths."""

    REGULAR = "regular"
    SYMLINK = "symlink"
    DIRECTORY = "directory"
    OTHER = "other"


def _validate_specimen_relative_path(v: Any, handler: Any, info: ValidationInfo) -> Path:
    """Validate snapshot-relative path with format and existence checks.

    WrapValidator that combines format validation, type coercion, and existence checking.

    Args:
        v: Input value (str or Path)
        handler: Pydantic's inner validator
        info: Validation info with context

    Returns:
        Validated Path object

    Raises:
        KeyError: If snapshots not in validation context
        ValueError: If path invalid (empty, absolute, parent refs, not found, not regular file)
    """
    # Convert to Path if needed
    if isinstance(v, str):
        p = Path(v)
    elif isinstance(v, Path):
        p = v
    else:
        # Let Pydantic's handler deal with invalid types
        p = handler(v)

    # Format validation (always required)
    if not p.parts:
        raise ValueError("Path cannot be empty")

    if p.is_absolute():
        raise ValueError(f"Path must be relative, got absolute: {p}")

    if ".." in p.parts:
        raise ValueError(f"Path cannot contain parent references (..): {p}")

    # Existence validation (only when snapshots is available)
    # Critiques parsed standalone (no context) skip this validation
    if info.context and "snapshots" in info.context:
        ctx = info.context["snapshots"]

        if p not in ctx.all_discovered_files:
            raise ValueError(f"Path not found in snapshot: {p}")

        if ctx.all_discovered_files[p] != FileType.REGULAR:
            raise ValueError(f"Path must be a regular file, got {ctx.all_discovered_files[p].value}: {p}")

    return p


SnapshotRelativePath = Annotated[
    Path, WrapValidator(_validate_specimen_relative_path), PlainSerializer(str, return_type=str, when_used="json")
]
"""Path type for snapshot-relative paths with strict validation.

Requires snapshots in validation context (raises KeyError if missing).

Validates:
- Path is relative (not absolute)
- Path has no parent references (..)
- Path is non-empty
- Path exists in snapshot's all_discovered_files
- Path is a regular file (not directory/symlink/other)

Serializes to string in JSON output.
"""
