from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class LineRange(BaseModel):
    start_line: int = Field(..., ge=1, description="1-based start line number")
    end_line: int | None = Field(
        default=None, description="1-based end line number (inclusive); omit for single-line anchor"
    )

    @model_validator(mode="after")
    def _validate_range(self) -> LineRange:
        if self.start_line < 1:
            raise ValueError("start_line must be >= 1")
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line when provided")
        return self

    model_config = ConfigDict(extra="forbid")


class Occurrence(BaseModel):
    """One occurrence of an Issue.

    Authoring guidance:
    - An Issue represents one logical problem (id, rationale, properties). Use
      one Issue with multiple Occurrences when the same logical problem appears
      in several places but should be tracked together.
    - Prefer Occurrence-level notes for location-specific guidance; keep the
      Issue.rationale for the global explanation and acceptance criteria.
    """

    files: dict[Annotated[str, StringConstraints(pattern=r"^[^\n]+$")], list[LineRange] | None] = Field(
        description=(
            "Maps file paths -> list of LineRanges within that file or `None` to indicate an unspecified anchor in the file. "
            + "One Occurrence may reference multiple files (e.g., multi-file code fragment) but represents a single logical location instance."
        )
    )
    note: str | None = Field(
        default=None,
        description=(
            "Occurrence-specific explanatory note, for details unique to this occurrence; "
            "do not repeat issue-level rationale here."
        ),
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _validate_files_keys(self) -> Occurrence:
        # TODO(mpokorny): During grading/runs, validate that each path resolves within the hydrated specimen root.
        for k in self.files or {}:
            if k in {"paths", ""}:
                raise ValueError(f"Invalid files key: {k!r} — expected a real file path")
        return self


class SpecimenIssuesLoadError(Exception):
    """Raised when per-issue Jsonnet evaluation/validation yields any errors in strict mode.

    Carries a list of human-readable error lines. __str__ joins them with newlines
    so pytest and CLIs surface a readable summary.
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(str(self))

    def __str__(self) -> str:  # pragma: no cover - exercised via message rendering
        return "Specimen issue loading errors:\n" + "\n".join(self.errors)


# Strongly-typed identifiers with validation
IssueId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{0,200}$", min_length=1)]


class IssueCore(BaseModel):
    """Issue metadata without occurrences.

    Minimal header describing a logical problem (no property IDs linkage).
    When sending or storing per-location data separately, pair an IssueCore with
    one or more Occurrence objects rather than repeating metadata.
    """

    id: IssueId
    should_flag: bool
    rationale: str
    gap_note: str | None = Field(
        None,
        description=(
            "Documents gaps in the property taxonomy: when this finding relates to existing properties "
            "but also represents a generalizable principle that deserves its own property definition. "
            "Describes what property SHOULD exist to more precisely capture this pattern. "
            "Example: 'This pattern deserves a more specific property like \"fail-fast-on-missing-explicit-inputs\" "
            "to distinguish intentionally-missing vs user-explicitly-provided-but-missing files, rather than "
            "relying on the generic \"no-swallowing-errors\" property.'"
        )
    )

    model_config = ConfigDict(extra="forbid")
