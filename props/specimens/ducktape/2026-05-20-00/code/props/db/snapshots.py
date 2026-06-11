"""Database Pydantic models stored in JSONB columns."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LocationAnchor(BaseModel):
    """A specific location in a codebase snapshot.

    Used in both database storage (JSONB) and API responses.
    """

    file: str = Field(description="File path (relative to snapshot root)")
    start_line: int | None = Field(default=None, ge=1, description="Optional start line (1-based)")
    end_line: int | None = Field(default=None, ge=1, description="Optional end line (inclusive)")
    note: str | None = Field(default=None, description="Optional per-location note (e.g. 'definition site')")

    model_config = ConfigDict(extra="forbid", frozen=True)


class DBCriticSubmitPayload(BaseModel):
    """Database representation of critic submit payload.

    Issues are stored in normalized reported_issues table, not here.
    Access via critic_run.reported_issues ORM relationship.
    """

    notes_md: str | None = Field(default=None, description="Optional Markdown notes")

    model_config = ConfigDict(extra="forbid", frozen=True)
