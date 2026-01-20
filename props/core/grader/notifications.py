"""Pydantic models for grader pg_notify notifications.

These models define the schema for notifications sent by PostgreSQL triggers
and consumed by grader daemons.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from props.core.ids import SnapshotSlug

# pg_notify channel for grading-related events
GRADING_PENDING_CHANNEL = "grading_pending"


class Operation(StrEnum):
    """Database operation that triggered the notification."""

    INSERT = "INSERT"
    DELETE = "DELETE"


# =============================================================================
# Event types (discriminated union by table)
# =============================================================================

# Ground truth tables (from notify_gt_changed trigger)


class TruePositivesEvent(BaseModel):
    """True positive change. PK: (snapshot_slug, tp_id)."""

    table: Literal["true_positives"] = "true_positives"
    operation: Operation
    tp_id: str


class TruePositiveOccurrencesEvent(BaseModel):
    """True positive occurrence change. PK: (snapshot_slug, tp_id, occurrence_id)."""

    table: Literal["true_positive_occurrences"] = "true_positive_occurrences"
    operation: Operation
    tp_id: str
    occurrence_id: str


class FalsePositivesEvent(BaseModel):
    """False positive change. PK: (snapshot_slug, fp_id)."""

    table: Literal["false_positives"] = "false_positives"
    operation: Operation
    fp_id: str


class FalsePositiveOccurrencesEvent(BaseModel):
    """False positive occurrence change. PK: (snapshot_slug, fp_id, occurrence_id)."""

    table: Literal["false_positive_occurrences"] = "false_positive_occurrences"
    operation: Operation
    fp_id: str
    occurrence_id: str


# Critique tables (from notify_critique_changed trigger)


class ReportedIssuesEvent(BaseModel):
    """Reported issue change. PK: (agent_run_id, issue_id)."""

    table: Literal["reported_issues"] = "reported_issues"
    operation: Operation
    agent_run_id: UUID
    issue_id: str


class ReportedIssueOccurrencesEvent(BaseModel):
    """Reported issue occurrence change. PK: (occurrence_id), FK: (agent_run_id, reported_issue_id)."""

    table: Literal["reported_issue_occurrences"] = "reported_issue_occurrences"
    operation: Operation
    occurrence_id: int
    agent_run_id: UUID
    reported_issue_id: str


GradingEvent = Annotated[
    TruePositivesEvent
    | TruePositiveOccurrencesEvent
    | FalsePositivesEvent
    | FalsePositiveOccurrencesEvent
    | ReportedIssuesEvent
    | ReportedIssueOccurrencesEvent,
    Field(discriminator="table"),
]


# =============================================================================
# Notification model
# =============================================================================


class GradingPendingNotification(BaseModel):
    """Notification sent when grading work is needed.

    Produced by PostgreSQL triggers on:
    - Ground truth changes: notify_gt_changed() on TP/FP INSERT/DELETE
    - Critique changes: notify_critique_changed() on reported_issues/occurrences INSERT

    Consumed by: GraderDaemonScaffold, DaemonState in daemon_main.py
    """

    snapshot_slug: SnapshotSlug
    event: GradingEvent
