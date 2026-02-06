"""Grader agent tool argument and result models."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

# --- Ground truth reference types (discriminated union) ---


class TPRef(OpenAIStrictModeBaseModel):
    """Reference to a true positive occurrence."""

    type: Literal["tp"] = "tp"
    tp_id: str = Field(..., description="True positive ID")
    occurrence_id: str = Field(..., description="Occurrence ID")


class FPRef(OpenAIStrictModeBaseModel):
    """Reference to a false positive occurrence."""

    type: Literal["fp"] = "fp"
    fp_id: str = Field(..., description="False positive ID")
    occurrence_id: str = Field(..., description="Occurrence ID")


GTRef = Annotated[TPRef | FPRef, Field(discriminator="type")]


# --- Tool argument models ---


class ListPendingArgs(OpenAIStrictModeBaseModel):
    issue: str | None = Field(None, description="Filter to specific critique issue ID")
    gt: GTRef | None = Field(None, description="Filter to specific GT occurrence")
    run: UUID | None = Field(None, description="Filter to specific critic run ID")


class ShowIssueArgs(OpenAIStrictModeBaseModel):
    run: UUID = Field(..., description="Critic run ID")
    issue_id: str = Field(..., description="Critique issue ID to show")


class ShowTPArgs(OpenAIStrictModeBaseModel):
    tp_id: str = Field(..., description="True positive ID")
    occurrence_id: str = Field(..., description="Occurrence ID")


class ShowFPArgs(OpenAIStrictModeBaseModel):
    fp_id: str = Field(..., description="False positive ID")
    occurrence_id: str = Field(..., description="Occurrence ID")


class EdgeSpec(OpenAIStrictModeBaseModel):
    """A single edge specification for insert_edges."""

    gt_ref: GTRef = Field(..., description="GT reference")
    credit: float = Field(..., ge=0.0, le=1.0, description="Credit value 0.0-1.0")


class InsertEdgesArgs(OpenAIStrictModeBaseModel):
    run: UUID = Field(..., description="Critic run ID")
    issue_id: str = Field(..., description="Critique issue ID")
    rationale: str = Field(..., description="Explanation for the matches")
    edges: list[EdgeSpec] = Field(..., description="List of edges to create")


class FillRemainingArgs(OpenAIStrictModeBaseModel):
    run: UUID = Field(..., description="Critic run ID")
    issue_id: str = Field(..., description="Critique issue ID")
    expected_count: int = Field(..., description="Expected number of edges to fill (safety check)")
    rationale: str = Field(..., description="Explanation for why these don't match")


class DeleteEdgesArgs(OpenAIStrictModeBaseModel):
    run: UUID = Field(..., description="Critic run ID")
    issue_id: str = Field(..., description="Critique issue ID")


class SleepArgs(OpenAIStrictModeBaseModel):
    summary: str = Field(..., description="Brief summary of what was accomplished this round")


class ReportFailureArgs(OpenAIStrictModeBaseModel):
    message: str = Field(..., description="Description of why grading could not be completed")


# --- Result models ---


class PendingEdge(BaseModel):
    """A pending grading edge from grading_pending view."""

    critique_run_id: UUID
    critique_issue_id: str
    snapshot_slug: str
    gt_ref: GTRef


class LocationInfo(BaseModel):
    """Location information for an occurrence."""

    file: str
    start_line: int | None
    end_line: int | None


class IssueDetails(BaseModel):
    """Details of a critique issue."""

    issue_id: str
    critique_run_id: UUID
    rationale: str
    locations: list[LocationInfo]


class GTDetails(BaseModel):
    """Details of a ground truth occurrence."""

    gt_ref: GTRef
    rationale: str
    files: dict[str, tuple[int | None, int | None]]  # file -> (start, end)
    note: str | None
