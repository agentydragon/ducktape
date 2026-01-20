"""Pydantic models for grader pg_notify notifications.

These models define the schema for notifications sent by PostgreSQL triggers
and consumed by grader daemons.

Notification structure: {operation, item: {table (discriminator), key columns}, snapshot_slug}
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
# Item types (discriminated union by table)
# =============================================================================

# Ground truth tables (from notify_gt_changed trigger)


class TruePositivesItem(BaseModel):
    """True positive. PK: (snapshot_slug, tp_id)."""

    table: Literal["true_positives"] = "true_positives"
    tp_id: str


class TruePositiveOccurrencesItem(BaseModel):
    """True positive occurrence. PK: (snapshot_slug, tp_id, occurrence_id)."""

    table: Literal["true_positive_occurrences"] = "true_positive_occurrences"
    tp_id: str
    occurrence_id: str


class FalsePositivesItem(BaseModel):
    """False positive. PK: (snapshot_slug, fp_id)."""

    table: Literal["false_positives"] = "false_positives"
    fp_id: str


class FalsePositiveOccurrencesItem(BaseModel):
    """False positive occurrence. PK: (snapshot_slug, fp_id, occurrence_id)."""

    table: Literal["false_positive_occurrences"] = "false_positive_occurrences"
    fp_id: str
    occurrence_id: str


# Critique tables (from notify_critique_changed trigger)


class ReportedIssuesItem(BaseModel):
    """Reported issue. PK: (agent_run_id, issue_id)."""

    table: Literal["reported_issues"] = "reported_issues"
    agent_run_id: UUID
    issue_id: str


class ReportedIssueOccurrencesItem(BaseModel):
    """Reported issue occurrence. PK: (occurrence_id), FK: (agent_run_id, reported_issue_id)."""

    table: Literal["reported_issue_occurrences"] = "reported_issue_occurrences"
    occurrence_id: int
    agent_run_id: UUID
    reported_issue_id: str


GradingItem = Annotated[
    TruePositivesItem
    | TruePositiveOccurrencesItem
    | FalsePositivesItem
    | FalsePositiveOccurrencesItem
    | ReportedIssuesItem
    | ReportedIssueOccurrencesItem,
    Field(discriminator="table"),
]


# =============================================================================
# Notification model
# =============================================================================


class GradingPendingNotification(BaseModel):
    """Notification sent when grading work is needed.

    Structure: {operation, item: {table, ...key_columns}, snapshot_slug}

    Produced by PostgreSQL triggers on:
    - Ground truth changes: notify_gt_changed() on TP/FP INSERT/DELETE
    - Critique changes: notify_critique_changed() on reported_issues/occurrences INSERT

    Consumed by: GraderDaemonScaffold, DaemonState in daemon_main.py
    """

    operation: Operation
    item: GradingItem
    snapshot_slug: SnapshotSlug
