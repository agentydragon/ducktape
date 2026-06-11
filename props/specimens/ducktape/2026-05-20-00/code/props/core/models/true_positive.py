from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator


class LineRange(BaseModel):
    start_line: int = Field(ge=1, description="1-based start line number")
    end_line: int | None = Field(description="1-based end line number (inclusive); None for single-line anchor")
    note: str | None = Field(
        default=None,
        description=(
            "Optional per-range note explaining why this specific range matters "
            "(e.g., 'definition site' vs 'call site' within the same occurrence)"
        ),
    )
    # NOTE: No default on end_line for OpenAI strict mode compatibility (used in MCP inputs)
    # TODO: Decouple DB state and MCP I/O shapes so database models can have sensible defaults
    # while MCP inputs maintain strict mode compliance

    @model_validator(mode="after")
    def _validate_range(self) -> LineRange:
        if self.start_line < 1:
            raise ValueError("start_line must be >= 1")
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line when provided")
        return self

    def format(self) -> str:
        """Format line range as string (e.g., '123' or '123-145')."""
        if self.end_line is None:
            return str(self.start_line)
        return f"{self.start_line}-{self.end_line}"

    model_config = ConfigDict(extra="forbid")


class FileOccurrence(BaseModel):
    """One file in an occurrence (for OpenAI strict mode compatibility)."""

    path: Path = Field(description="File path")
    ranges: list[LineRange] | None = Field(default=None, description="Line ranges or None for unspecified")
    model_config = ConfigDict(extra="forbid")


class Occurrence(BaseModel):
    """One occurrence of a TruePositive.

    Authoring guidance:
    - A TruePositive represents one logical problem (id, rationale, properties). Use
      one TruePositive with multiple Occurrences when the same logical problem appears
      in several places but should be tracked together.
    - Prefer Occurrence-level notes for location-specific guidance; keep the
      TruePositive.rationale for the global explanation and acceptance criteria.
    """

    files: list[FileOccurrence] = Field(
        description=(
            "List of files with their line ranges. "
            + "One Occurrence may reference multiple files (e.g., multi-file code fragment) but represents a single logical location instance."
        )
    )
    note: str | None = Field(
        description=(
            "Occurrence-specific explanatory note, for details unique to this occurrence; "
            "do not repeat issue-level rationale here."
        )
    )

    model_config = ConfigDict(extra="forbid")


class TruePositiveOccurrence(BaseModel):
    """One occurrence of a true positive.

    For true positives, each occurrence tracks minimal file sets required for detection.
    """

    occurrence_id: str = Field(description="Unique ID within this TP (e.g., 'occ-0', 'occ-1')")
    files: dict[Path, list[LineRange] | None] = Field(
        description="Maps file paths to line ranges or None for unspecified anchor"
    )
    note: str | None = Field(default=None, description="Occurrence-specific note")
    critic_scopes_expected_to_recall: set[frozenset[Path]] = Field(
        description=(
            "BENCHMARK NORMALIZATION: Critic scope file sets where this occurrence counts toward recall. "
            "If ANY of these file sets is a subset of the critic's reviewed scope, this occurrence "
            "is included in the recall denominator. "
            "Outer set = alternatives (OR), inner frozenset = required together (AND). "
            "Must be non-empty. "
            "\n\n"
            "Detection standard: 'If I gave a high-quality critic this file set to review, "
            "and they failed to find this issue, would that be a failure on their part?' "
            "A thorough code review starting from these files naturally includes following "
            "imports/calls, checking for existing patterns, and searching for duplication. "
            "Not 'can you detect this reading only these files in isolation'."
        )
    )
    match_file_restriction: set[Path] | None = Field(
        description=(
            "GRADING OPTIMIZATION: Restricts which critique outputs can match this occurrence. "
            "If set, a critique reporting issues only in files OUTSIDE this set will be skipped "
            "during matching (assumed non-match without semantic comparison). "
            "\n\n"
            "NULL = allow matching from any file (unrestricted). This is the conservative default when we haven't "
            "determined the closed set of valid reporting files, OR for issues not bound to specific files. "
            "Non-empty = we know the closed set; skip matching if critique's files don't overlap. "
            "\n\n"
            "Independent of critic_scopes_expected_to_recall (detection source ≠ valid reporting targets). "
            "Example: 'X.py calls Y.abort() which doesn't exist' - detectable from X.py, but validly "
            "reported in either X.py (caller) or Y.py (missing method)."
        )
    )

    @field_serializer("critic_scopes_expected_to_recall")
    def serialize_critic_scopes_expected_to_recall(self, value: set[frozenset[Path]]) -> list[list[str]]:
        """Convert set[frozenset[Path]] to JSON: list[list[str]]."""
        return [[str(p) for p in fs] for fs in value]

    @field_serializer("files", when_used="json")
    def _serialize_files(self, value: dict[Path, list[LineRange] | None]) -> dict[str, Any]:
        """Convert Path keys to strings for JSON serialization."""
        return {str(k): v for k, v in value.items()}

    @model_validator(mode="after")
    def validate_non_empty(self) -> TruePositiveOccurrence:
        if not self.critic_scopes_expected_to_recall:
            raise ValueError("critic_scopes_expected_to_recall must be non-empty")
        if self.match_file_restriction is not None and not self.match_file_restriction:
            raise ValueError("match_file_restriction must be None or non-empty")
        return self

    model_config = ConfigDict(extra="forbid")


class FalsePositiveOccurrence(BaseModel):
    """One occurrence of a false positive.

    For false positives, we track which files make this occurrence relevant.
    """

    occurrence_id: str = Field(description="Unique ID within this FP (e.g., 'occ-0', 'occ-1')")
    files: dict[Path, list[LineRange] | None] = Field(
        description="Maps file paths to line ranges or None for unspecified anchor"
    )
    note: str | None = Field(default=None, description="Occurrence-specific note")
    relevant_files: set[Path] = Field(description="Files that make this FP relevant (ANY logic). Must be non-empty.")
    match_file_restriction: set[Path] | None = Field(
        description=(
            "Files a critique must report on to match this occurrence (sparse grading). "
            "NULL = unrestricted (any critique can match). Non-empty = file-restricted."
        )
    )

    @field_serializer("relevant_files")
    def serialize_relevant_files(self, value: set[Path]) -> list[str]:
        """Convert set[Path] to JSON: list[str]."""
        return [str(p) for p in value]

    @field_serializer("files", when_used="json")
    def _serialize_files(self, value: dict[Path, list[LineRange] | None]) -> dict[str, Any]:
        """Convert Path keys to strings for JSON serialization."""
        return {str(k): v for k, v in value.items()}

    @model_validator(mode="after")
    def validate_non_empty(self) -> FalsePositiveOccurrence:
        if not self.relevant_files:
            raise ValueError("relevant_files must be non-empty")
        if self.match_file_restriction is not None and not self.match_file_restriction:
            raise ValueError("match_file_restriction must be None or non-empty")
        return self

    model_config = ConfigDict(extra="forbid")
