"""Export ground truth from database to YAML files.

Inverse of sync: reads TPs/FPs from DB and writes YAML files to specimens repo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from props.core.ids import SnapshotSlug
from props.db.models import (
    CriticScopeExpectedToRecall,
    FalsePositive,
    FalsePositiveOccurrenceORM,
    FileSet,
    FileSetMember,
    OccurrenceRangeORM,
    TruePositive,
    TruePositiveOccurrenceORM,
)


class LiteralStr(str):
    """String that should be rendered as YAML literal block."""

    __slots__ = ()


def _literal_str_representer(dumper: yaml.Dumper, data: LiteralStr) -> yaml.Node:
    """Represent LiteralStr as YAML literal block style."""
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


yaml.add_representer(LiteralStr, _literal_str_representer)


def _get_file_set_paths(session: Session, snapshot_slug: SnapshotSlug, files_hash: str) -> list[str]:
    """Get sorted file paths for a file set hash."""
    members = (
        session.execute(
            select(FileSetMember.file_path)
            .where(FileSetMember.snapshot_slug == snapshot_slug, FileSetMember.files_hash == files_hash)
            .order_by(FileSetMember.file_path)
        )
        .scalars()
        .all()
    )
    return list(members)


def _format_line_ranges(
    ranges: list[OccurrenceRangeORM],
) -> list[dict[str, Any]] | list[list[int]] | list[int] | int | None:
    """Convert ORM line ranges to YAML-friendly format.

    YAML format:
      - If any range has a note: [{"start_line": 10, "end_line": 20, "note": "..."}, ...]
      - Single line where start == end: int (e.g., 42)
      - Single range: [start, end] (e.g., [10, 20])
      - Multiple ranges: [[start, end], ...] (e.g., [[10, 20], [30, 40]])
    """
    if not ranges:
        return None

    # If any range has a note, use dict format for all ranges
    if any(r.note for r in ranges):
        return [
            {
                k: v
                for k, v in {"start_line": r.start_line, "end_line": r.end_line, "note": r.note}.items()
                if v is not None
            }
            for r in ranges
        ]

    formatted = [[r.start_line, r.end_line] for r in ranges]

    # Simplify single ranges
    if len(formatted) == 1:
        start, end = formatted[0]
        if start == end:
            return start  # Single line as int
        return [start, end]  # Single range as [start, end]

    return formatted  # Multiple ranges as [[start, end], ...]


def _format_files(ranges: list[OccurrenceRangeORM]) -> dict[str, Any]:
    """Convert ORM ranges to YAML-friendly files dict.

    Groups ranges by file path and sorts for deterministic output.
    """
    # Group ranges by file path
    by_file: dict[str, list[OccurrenceRangeORM]] = {}
    for range_orm in ranges:
        path_str = str(range_orm.file_path)
        if path_str not in by_file:
            by_file[path_str] = []
        by_file[path_str].append(range_orm)

    # Format each file's ranges
    result: dict[str, Any] = {}
    for path in sorted(by_file.keys()):
        result[path] = _format_line_ranges(by_file[path])
    return result


def _maybe_literal(text: str) -> str | LiteralStr:
    """Use literal block style for multi-line strings, plain for single-line."""
    if "\n" in text:
        return LiteralStr(text)
    return text


def _export_tp_occurrence(session: Session, occ: TruePositiveOccurrenceORM) -> dict[str, Any]:
    """Export a single TP occurrence to YAML dict."""
    result: dict[str, Any] = {"occurrence_id": occ.occurrence_id, "files": _format_files(occ.ranges)}

    if occ.note:
        result["note"] = _maybe_literal(occ.note)

    # Get critic_scopes_expected_to_recall from relationship
    # Each scope's paths are sorted, then scopes are sorted by first path
    critic_scopes_expected_to_recall: list[list[str]] = []
    for scope in occ.critic_scopes_expected_to_recall:
        if scope.file_set:
            paths = sorted(m.file_path for m in scope.file_set.members)
            critic_scopes_expected_to_recall.append(paths)

    if critic_scopes_expected_to_recall:
        # Sort scopes for deterministic output
        critic_scopes_expected_to_recall.sort(key=lambda x: x[0] if x else "")
        result["critic_scopes_expected_to_recall"] = critic_scopes_expected_to_recall

    # Get graders_match_only_if_reported_on if set
    if occ.graders_match_only_if_reported_on:
        paths = _get_file_set_paths(session, occ.snapshot_slug, occ.graders_match_only_if_reported_on)
        if paths:
            result["graders_match_only_if_reported_on"] = paths

    return result


def _export_fp_occurrence(session: Session, occ: FalsePositiveOccurrenceORM) -> dict[str, Any]:
    """Export a single FP occurrence to YAML dict."""
    result: dict[str, Any] = {"occurrence_id": occ.occurrence_id, "files": _format_files(occ.ranges)}

    if occ.note:
        result["note"] = _maybe_literal(occ.note)

    # relevant_files from relevant_file_orms relationship
    if occ.relevant_file_orms:
        result["relevant_files"] = sorted(str(rf.file_path) for rf in occ.relevant_file_orms)

    # Get graders_match_only_if_reported_on if set
    if occ.graders_match_only_if_reported_on:
        paths = _get_file_set_paths(session, occ.snapshot_slug, occ.graders_match_only_if_reported_on)
        if paths:
            result["graders_match_only_if_reported_on"] = paths

    return result


def export_true_positive(session: Session, tp: TruePositive) -> dict[str, Any]:
    """Export a TruePositive to YAML-serializable dict."""
    return {
        "rationale": _maybe_literal(tp.rationale),
        "should_flag": True,
        "occurrences": [_export_tp_occurrence(session, occ) for occ in tp.occurrences],
    }


def export_false_positive(session: Session, fp: FalsePositive) -> dict[str, Any]:
    """Export a FalsePositive to YAML-serializable dict."""
    return {
        "rationale": _maybe_literal(fp.rationale),
        "should_flag": False,
        "occurrences": [_export_fp_occurrence(session, occ) for occ in fp.occurrences],
    }


class ExportResult:
    """Result of exporting ground truth for a snapshot."""

    def __init__(self, snapshot_slug: SnapshotSlug, tp_count: int, fp_count: int, output_dir: Path):
        self.snapshot_slug = snapshot_slug
        self.tp_count = tp_count
        self.fp_count = fp_count
        self.output_dir = output_dir

    @property
    def total_count(self) -> int:
        return self.tp_count + self.fp_count


def export_snapshot_issues(session: Session, snapshot_slug: SnapshotSlug, output_dir: Path) -> ExportResult:
    """Export all TPs and FPs for a snapshot to YAML files.

    Creates output_dir/issues/ with one YAML file per issue.
    File naming: {tp_id}.yaml or {fp_id}.yaml

    Args:
        session: Database session
        snapshot_slug: Snapshot to export
        output_dir: Root directory for output (issues/ subdirectory will be created)

    Returns:
        ExportResult with counts and output location
    """
    issues_dir = output_dir / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)

    # Export TPs with eager loading of relationships
    tps = (
        session.execute(
            select(TruePositive)
            .where(TruePositive.snapshot_slug == snapshot_slug)
            .options(
                selectinload(TruePositive.occurrences)
                .selectinload(TruePositiveOccurrenceORM.critic_scopes_expected_to_recall)
                .selectinload(CriticScopeExpectedToRecall.file_set)
                .selectinload(FileSet.members)
            )
            .order_by(TruePositive.tp_id)
        )
        .scalars()
        .all()
    )

    for tp in tps:
        data = export_true_positive(session, tp)
        output_path = issues_dir / f"{tp.tp_id}.yaml"
        with output_path.open("w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    # Export FPs with eager loading
    fps = (
        session.execute(
            select(FalsePositive)
            .where(FalsePositive.snapshot_slug == snapshot_slug)
            .options(selectinload(FalsePositive.occurrences))
            .order_by(FalsePositive.fp_id)
        )
        .scalars()
        .all()
    )

    for fp in fps:
        data = export_false_positive(session, fp)
        output_path = issues_dir / f"{fp.fp_id}.yaml"
        with output_path.open("w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return ExportResult(snapshot_slug=snapshot_slug, tp_count=len(tps), fp_count=len(fps), output_dir=output_dir)
