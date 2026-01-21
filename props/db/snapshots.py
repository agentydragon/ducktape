"""Database-specific models for canonical issue snapshots.

These models are the persistence layer for issue data and are intentionally
decoupled from MCP I/O models (grader.models.*) to avoid coupling database
migrations to protocol changes.

Key differences from MCP models:
- All Path objects stored as strings
- All sets stored as lists (simpler JSON representation)
- No complex types like NewType wrappers
- No Pydantic validators (data already validated before storage)
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DBLineRange(BaseModel):
    """Database representation of a line range."""

    start_line: int = Field(ge=1)
    end_line: int | None = Field(default=None)
    note: str | None = Field(default=None)

    model_config = ConfigDict(extra="forbid", frozen=True)


class DBLocationAnchor(BaseModel):
    """Database representation of a location anchor for reported issues.

    Matches the YAML ground truth format:
    - file: required file path
    - start_line: optional line number (1-based)
    - end_line: optional end line (inclusive)
    """

    file: str = Field(description="File path (relative to snapshot root)")
    start_line: int | None = Field(default=None, ge=1, description="Optional start line (1-based)")
    end_line: int | None = Field(default=None, ge=1, description="Optional end line (inclusive)")

    model_config = ConfigDict(extra="forbid", frozen=True)


class DBTruePositiveOccurrence(BaseModel):
    """Database representation of a true positive occurrence."""

    occurrence_id: str = Field(description="Unique ID within this TP")
    files: dict[str, list[DBLineRange] | None] = Field(description="File paths (as strings) mapped to line ranges")
    note: str | None = Field(default=None)
    critic_scopes_expected_to_recall: list[list[str]] = Field(
        description="Critic scope file sets where this counts toward recall (list of alternatives)"
    )

    model_config = ConfigDict(extra="forbid", frozen=True)


class DBFalsePositiveOccurrence(BaseModel):
    """Database representation of a false positive occurrence."""

    occurrence_id: str = Field(description="Unique ID within this FP")
    files: dict[str, list[DBLineRange] | None] = Field(description="File paths (as strings) mapped to line ranges")
    note: str | None = Field(default=None)
    relevant_files: list[str] = Field(description="Files that make this FP relevant")

    model_config = ConfigDict(extra="forbid", frozen=True)


class DBTruePositiveIssue(BaseModel):
    """Database representation of a true positive issue.

    This is the persisted form, decoupled from MCP I/O types.
    """

    id: str = Field(description="Issue ID (stored as string)")
    rationale: str = Field(description="Issue rationale (stored as string)")
    occurrences: list[DBTruePositiveOccurrence]

    model_config = ConfigDict(extra="forbid", frozen=True)


class DBKnownFalsePositive(BaseModel):
    """Database representation of a known false positive.

    This is the persisted form, decoupled from MCP I/O types.
    """

    id: str = Field(description="False positive ID (stored as string)")
    rationale: str = Field(description="FP rationale (stored as string)")
    occurrences: list[DBFalsePositiveOccurrence]

    model_config = ConfigDict(extra="forbid", frozen=True)


class DBCriticSubmitPayload(BaseModel):
    """Database representation of critic submit payload.

    Issues are stored in normalized reported_issues table, not here.
    Access via critic_run.reported_issues ORM relationship.
    """

    notes_md: str | None = Field(default=None, description="Optional Markdown notes")

    model_config = ConfigDict(extra="forbid", frozen=True)
