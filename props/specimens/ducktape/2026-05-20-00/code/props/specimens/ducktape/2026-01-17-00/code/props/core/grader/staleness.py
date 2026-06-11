"""Utilities for detecting stale grader runs."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any
from uuid import UUID

import canonicaljson

from props.core.agent_types import AgentType
from props.core.db.models import AgentRun, CanonicalIssuesSnapshot, FileSet, Snapshot as DBSnapshot
from props.core.db.session import Session, get_session
from props.core.db.snapshots import DBKnownFalsePositive, DBTruePositiveIssue
from props.core.grader.persistence import orm_fp_to_db, orm_tp_to_db
from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleSpec, SingleFileSetExample, WholeSnapshotExample


def resolve_scope_files(example_spec: ExampleSpec, session: Session) -> set[Path]:
    """Resolve an ExampleSpec to concrete file set.

    Args:
        example_spec: The example specification (discriminated union)
        session: Database session (required to avoid accidental nested sessions)

    Returns:
        Set of file paths in the scope
    """
    if isinstance(example_spec, WholeSnapshotExample):
        snapshot = session.query(DBSnapshot).filter_by(slug=example_spec.snapshot_slug).one()
        return snapshot.files_with_issues()
    if isinstance(example_spec, SingleFileSetExample):
        file_set = (
            session.query(FileSet)
            .filter_by(snapshot_slug=example_spec.snapshot_slug, files_hash=example_spec.files_hash)
            .one()
        )
        return {Path(m.file_path) for m in file_set.members}
    raise ValueError(f"Unknown scope type: {type(example_spec)}")


def filter_tps_in_expected_recall_scope(
    tps: list[DBTruePositiveIssue], targeted_files: set[Path]
) -> list[DBTruePositiveIssue]:
    """Filter TPs to those in expected recall scope for targeted_files.

    Returns TPs where at least one critic_scopes_expected_to_recall entry is a subset
    of targeted_files. These TPs count toward the recall denominator for this scope.

    NOTE: This determines recall DENOMINATOR only. Critics CAN find TPs outside expected
    scopes (achieving >100% recall). This is separate from graders_match_only_if_reported_on
    which is a hard constraint on where graders can give credit.
    """
    targeted_files_str = {str(p) for p in targeted_files}

    def is_in_expected_recall_scope(tp: DBTruePositiveIssue) -> bool:
        return any(
            set(scope) <= targeted_files_str
            for occurrence in tp.occurrences
            for scope in occurrence.critic_scopes_expected_to_recall
        )

    return [tp for tp in tps if is_in_expected_recall_scope(tp)]


def filter_relevant_db_fps(fps: list[DBKnownFalsePositive], targeted_files: set[Path]) -> list[DBKnownFalsePositive]:
    """Filter DB persistence FPs to only those relevant to targeted_files.

    Works on DB persistence models (for filtering stored snapshots in staleness check).
    """
    targeted_files_str = {str(p) for p in targeted_files}

    def is_relevant(fp: DBKnownFalsePositive) -> bool:
        return any(bool(set(occ.relevant_files) & targeted_files_str) for occ in fp.occurrences)

    return [fp for fp in fps if is_relevant(fp)]


def load_current_canonical_issues_from_db(
    snapshot_slug: SnapshotSlug, targeted_files: set[Path], session: Session
) -> dict[str, Any]:
    """Load current canonical TPs+FPs from database, filtered to targeted_files.

    Args:
        snapshot_slug: Snapshot to load issues from
        targeted_files: Files to filter issues by
        session: Database session (required to avoid accidental nested sessions)
    """
    snapshot = session.query(DBSnapshot).filter_by(slug=snapshot_slug).one()

    # Convert ORM models to DB persistence models first
    all_db_tps = [orm_tp_to_db(tp) for tp in snapshot.true_positives]
    all_db_fps = [orm_fp_to_db(fp) for fp in snapshot.false_positives]

    # Filter DB persistence models (single implementation shared with staleness check)
    tps_in_scope = filter_tps_in_expected_recall_scope(all_db_tps, targeted_files)
    relevant_db_fps = filter_relevant_db_fps(all_db_fps, targeted_files)

    # Create snapshot from filtered DB persistence models
    current_snapshot = CanonicalIssuesSnapshot(true_positives=tps_in_scope, false_positives=relevant_db_fps)
    return current_snapshot.model_dump(mode="json")


def identify_stale_runs() -> tuple[list[UUID], dict[SnapshotSlug, dict[str, int]]]:
    """Identify stale grader runs by comparing stored canonical snapshots with current issues.

    Returns:
        Tuple of (stale_run_ids, by_snapshot_stats)
        - stale_run_ids: List of grader run UUIDs
        - by_snapshot_stats: Dict mapping snapshot_slug -> {"total": N, "stale": M}

    Note: All grader runs now have canonical_issues_snapshot (NOT NULL constraint enforced).
    Legacy runs without snapshots were cleaned up in Dec 2025.
    """
    stale_run_ids: list[UUID] = []
    by_snapshot: dict[SnapshotSlug, dict[str, int]] = defaultdict(lambda: {"total": 0, "stale": 0})
    current_canonical_cache: dict[ExampleSpec, dict[str, Any]] = {}

    with get_session() as session:
        # Two-phase approach: first get grader runs with their linked critic runs
        # Query AgentRun for graders
        grader_runs = (
            session.query(AgentRun)
            .filter(AgentRun.type_config["agent_type"].astext == AgentType.GRADER)
            .order_by(AgentRun.created_at.desc())
            .all()
        )

        # Batch-fetch all critic runs to avoid N+1 query
        graded_critic_run_ids = [run.grader_config().graded_agent_run_id for run in grader_runs]
        critic_runs_list = (
            session.query(AgentRun).filter(AgentRun.agent_run_id.in_(graded_critic_run_ids)).all()
            if graded_critic_run_ids
            else []
        )
        critic_runs_by_id = {run.agent_run_id: run for run in critic_runs_list}

        for grader_run in grader_runs:
            grader_config = grader_run.grader_config()
            stored_snapshot = grader_config.canonical_issues_snapshot
            graded_critic_run_id = grader_config.graded_agent_run_id

            # Get the critic run from lookup dict
            critic_run = critic_runs_by_id.get(graded_critic_run_id)
            if not critic_run:
                raise ValueError(f"Critic run {graded_critic_run_id} not found for grader {grader_run.agent_run_id}")

            critic_config = critic_run.critic_config()
            example_spec = critic_config.example
            snapshot_slug = example_spec.snapshot_slug

            by_snapshot[snapshot_slug]["total"] += 1

            # All grader runs must have canonical_issues_snapshot (enforced by NOT NULL constraint)
            # This assertion documents the database invariant
            if stored_snapshot is None:
                continue  # Skip runs without canonical snapshot

            # Parse stored snapshot from dict to model
            stored_snapshot_model = CanonicalIssuesSnapshot.model_validate(stored_snapshot)

            # Resolve scope specification to file set (pass session to avoid nested session issues)
            targeted_files = resolve_scope_files(example_spec, session)

            # Filter stored snapshot to TPs in expected recall scope and relevant FPs (same filtering applied at grading time)
            stored_tps_in_scope = filter_tps_in_expected_recall_scope(
                stored_snapshot_model.true_positives, targeted_files
            )
            relevant_stored_fps = filter_relevant_db_fps(stored_snapshot_model.false_positives, targeted_files)

            # Create filtered snapshot model and serialize
            filtered_stored = CanonicalIssuesSnapshot(
                true_positives=stored_tps_in_scope, false_positives=relevant_stored_fps
            )
            stored_canonical = filtered_stored.model_dump(mode="json")

            # Load current canonical issues (cached by example spec, pass session)
            # ExampleSpec is frozen/hashable so we can use it directly as cache key
            if example_spec not in current_canonical_cache:
                current_canonical_cache[example_spec] = load_current_canonical_issues_from_db(
                    snapshot_slug, targeted_files, session
                )
            current_canonical = current_canonical_cache[example_spec]

            # Compare canonical JSON representations
            stored_bytes = canonicaljson.encode_canonical_json(stored_canonical)
            current_bytes = canonicaljson.encode_canonical_json(current_canonical)

            if stored_bytes != current_bytes:
                stale_run_ids.append(grader_run.agent_run_id)
                by_snapshot[snapshot_slug]["stale"] += 1

    return stale_run_ids, by_snapshot
