"""Critic agent tool argument models.

Tool implementations are in props.core.agent_loop.loop (using DirectToolProvider).
This module only contains the Pydantic models for tool arguments.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class InsertIssueArgs(BaseModel):
    """Arguments for insert_issue tool."""

    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(..., description="Unique identifier for this issue (kebab-case slug)")
    rationale: str = Field(..., description="Explanation of why this is an issue")


class InsertOccurrenceArgs(BaseModel):
    """Arguments for insert_occurrence tool."""

    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(..., description="ID of the issue this occurrence belongs to")
    file: str = Field(..., description="File path relative to workspace root")
    start_line: int | None = Field(None, description="Starting line number")
    end_line: int | None = Field(None, description="Ending line number")


class LocationSpec(BaseModel):
    """A single location in insert_occurrence_multi."""

    model_config = ConfigDict(extra="forbid")

    file: str
    start_line: int | None = None
    end_line: int | None = None


class InsertOccurrenceMultiArgs(BaseModel):
    """Arguments for insert_occurrence_multi tool."""

    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(..., description="ID of the issue this occurrence belongs to")
    locations: list[LocationSpec] = Field(..., description="List of locations for this occurrence")


class DeleteIssueArgs(BaseModel):
    """Arguments for delete_issue tool."""

    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(..., description="ID of the issue to delete")


class SubmitArgs(BaseModel):
    """Arguments for submit tool."""

    model_config = ConfigDict(extra="forbid")

    issues_count: int = Field(..., description="Total number of issues reported")
    summary: str = Field(..., description="Brief summary of the code review findings")


class ReportFailureArgs(BaseModel):
    """Arguments for report_failure tool."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(..., description="Description of why the critique could not be completed")
