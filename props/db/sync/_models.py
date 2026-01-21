"""Internal dataclasses for sync operations.

⚠️⚠️⚠️ PRIVATE MODULE - DO NOT IMPORT OUTSIDE db/sync/ ⚠️⚠️⚠️

These are intermediate representations used during YAML → ORM conversion.
For runtime access, use ORM models from db.models.
"""

from __future__ import annotations

from dataclasses import dataclass

from props.core.ids import SnapshotSlug
from props.core.models.true_positive import FalsePositiveOccurrence, TruePositiveOccurrence
from props.core.models.types import Rationale


@dataclass
class TruePositive:
    """True positive issue (for sync only)."""

    tp_id: str
    snapshot_slug: SnapshotSlug
    rationale: Rationale
    occurrences: list[TruePositiveOccurrence]


@dataclass
class FalsePositive:
    """False positive (for sync only)."""

    fp_id: str
    snapshot_slug: SnapshotSlug
    rationale: Rationale
    occurrences: list[FalsePositiveOccurrence]
