"""Stats API routes for props dashboard.

All endpoints require admin access (localhost admin or authenticated admin user).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from props.backend.auth import require_admin_access
from props.core.agent_types import AgentType
from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleKind
from props.core.splits import Split
from props.db.examples import Example, count_available_examples_by_scope_all
from props.db.models import (
    AgentDefinition,
    AgentRunStatus,
    FileSetMember,
    RecallByDefinitionExample,
    RecallByDefinitionSplitKind,
    Snapshot,
    StatsWithCI,
)
from props.db.session import get_session

router = APIRouter(dependencies=[Depends(require_admin_access)])


class DefinitionInfo(BaseModel):
    image_digest: str
    agent_type: AgentType
    created_at: datetime


class DefinitionsResponse(BaseModel):
    definitions: list[DefinitionInfo]


class SplitScopeStats(BaseModel):
    recall_stats: StatsWithCI | None
    n_examples: int
    zero_count: int
    status_counts: dict[AgentRunStatus, int]
    total_available: int


# Nested: split -> example_kind -> stats
SplitStats = dict[Split, dict[ExampleKind, SplitScopeStats]]


class DefinitionRow(BaseModel):
    image_digest: str
    created_at: datetime
    stats: SplitStats


class OverviewResponse(BaseModel):
    definitions: list[DefinitionRow]
    example_counts: dict[Split, dict[ExampleKind, int]]
    total_definitions: int


def to_split_scope_stats(row: RecallByDefinitionSplitKind, total_available: int) -> SplitScopeStats:
    """Convert RecallByDefinitionSplitKind row to SplitScopeStats."""
    return SplitScopeStats(
        recall_stats=row.recall_stats,
        n_examples=row.n_examples or 0,
        zero_count=row.zero_count or 0,
        status_counts=Counter(row.status_counts or {}),
        total_available=total_available,
    )


@router.get("/overview")
def get_overview() -> OverviewResponse:
    with get_session() as session:
        example_counts = count_available_examples_by_scope_all(session, [Split.TRAIN, Split.VALID])

        # Get ALL critic definitions, not just those with stats
        all_definitions = (
            session.query(AgentDefinition)
            .filter(AgentDefinition.agent_type == AgentType.CRITIC)
            .order_by(AgentDefinition.created_at.desc())
            .limit(100)
            .all()
        )

        agg_results = (
            session.query(RecallByDefinitionSplitKind)
            .filter(RecallByDefinitionSplitKind.split.in_([Split.TRAIN, Split.VALID]))
            .all()
        )

        by_def: dict[str, dict[tuple[Split, ExampleKind], RecallByDefinitionSplitKind]] = defaultdict(dict)
        for row in agg_results:
            by_def[row.critic_image_digest][(row.split, row.example_kind)] = row

        def build_stats(def_id: str) -> SplitStats:
            result: SplitStats = defaultdict(dict)
            if def_id in by_def:
                for (split, kind), row in by_def[def_id].items():
                    result[split][kind] = to_split_scope_stats(row, example_counts.get((split, kind), 0))
            return dict(result)

        rows = [
            DefinitionRow(image_digest=d.digest, created_at=d.created_at, stats=build_stats(d.digest))
            for d in all_definitions
        ]

        # Convert example_counts to nested dict
        nested_counts: dict[Split, dict[ExampleKind, int]] = defaultdict(dict)
        for (s, k), v in example_counts.items():
            nested_counts[s][k] = v

        return OverviewResponse(definitions=rows, example_counts=dict(nested_counts), total_definitions=len(rows))


@router.get("/definitions")
def list_definitions(agent_type: AgentType | None = None) -> DefinitionsResponse:
    """List all agent definitions, optionally filtered by type."""
    with get_session() as session:
        query = session.query(AgentDefinition)
        if agent_type:
            query = query.filter_by(agent_type=agent_type)
        definitions = query.order_by(AgentDefinition.created_at.desc()).all()
        return DefinitionsResponse(
            definitions=[
                DefinitionInfo(image_digest=d.digest, agent_type=AgentType(d.agent_type), created_at=d.created_at)
                for d in definitions
            ]
        )


# Per-example stats for a definition
class ExampleStats(BaseModel):
    snapshot_slug: SnapshotSlug
    example_kind: ExampleKind
    files_hash: str | None
    split: Split
    recall_denominator: int
    n_runs: int
    status_counts: dict[AgentRunStatus, int]
    credit_stats: StatsWithCI | None


class DefinitionDetailResponse(BaseModel):
    image_digest: str
    agent_type: AgentType
    created_at: datetime
    stats: SplitStats
    examples: list[ExampleStats]


@router.get("/definitions/{image_digest}")
def get_definition_detail(image_digest: str) -> DefinitionDetailResponse:
    """Get detailed stats for a single definition including per-example breakdown."""
    with get_session() as session:
        definition = session.query(AgentDefinition).filter_by(id=image_digest).first()
        if not definition:
            raise HTTPException(status_code=404, detail=f"Definition not found: {image_digest}")

        example_counts = count_available_examples_by_scope_all(session, [Split.TRAIN, Split.VALID])

        # Get aggregate stats
        agg_results = (
            session.query(RecallByDefinitionSplitKind)
            .filter(RecallByDefinitionSplitKind.critic_image_digest == image_digest)
            .filter(RecallByDefinitionSplitKind.split.in_([Split.TRAIN, Split.VALID]))
            .all()
        )

        stats: SplitStats = defaultdict(dict)
        for row in agg_results:
            stats[row.split][row.example_kind] = to_split_scope_stats(
                row, example_counts.get((row.split, row.example_kind), 0)
            )

        # Get per-example breakdown
        example_results = (
            session.query(RecallByDefinitionExample)
            .filter(RecallByDefinitionExample.critic_image_digest == image_digest)
            .filter(RecallByDefinitionExample.split.in_([Split.TRAIN, Split.VALID]))
            .order_by(
                RecallByDefinitionExample.split,
                RecallByDefinitionExample.snapshot_slug,
                RecallByDefinitionExample.example_kind,
            )
            .all()
        )

        examples = [
            ExampleStats(
                snapshot_slug=r.snapshot_slug,
                example_kind=r.example_kind,
                files_hash=r.files_hash,
                split=r.split,
                recall_denominator=r.recall_denominator,
                n_runs=r.n_runs,
                status_counts=Counter(r.status_counts or {}),
                credit_stats=r.credit_stats,
            )
            for r in example_results
        ]

        return DefinitionDetailResponse(
            image_digest=definition.digest,
            agent_type=AgentType(definition.agent_type),
            created_at=definition.created_at,
            stats=dict(stats),
            examples=examples,
        )


class DefinitionStatsForExample(BaseModel):
    """Stats for a single definition on this example."""

    image_digest: str
    model: str
    n_runs: int
    status_counts: dict[AgentRunStatus, int]
    credit_stats: StatsWithCI | None


class ExampleDetailResponse(BaseModel):
    """Detailed view of a single example."""

    snapshot_slug: SnapshotSlug
    example_kind: ExampleKind
    files_hash: str | None
    split: Split
    recall_denominator: int
    files: list[str] | None  # For file_set examples
    definitions: list[DefinitionStatsForExample]  # Per-definition stats
    credit_stats: StatsWithCI | None  # Aggregate metrics across all definitions


@router.get("/examples")
def get_example_detail(
    snapshot_slug: SnapshotSlug, example_kind: ExampleKind, files_hash: str | None = None
) -> ExampleDetailResponse:
    """Get detailed information about a specific example.

    Query parameters:
        snapshot_slug: Snapshot identifier (e.g., "ducktape/2025-11-20-00")
        example_kind: Example kind (whole_snapshot or file_set)
        files_hash: Files hash (required for file_set, must be None for whole_snapshot)

    Returns:
        Detailed example information including:
        - Example metadata (split, recall_denominator)
        - File list (for file_set examples)
        - Per-definition run statistics
        - Aggregate metrics
    """
    with get_session() as session:
        # Validate and fetch the example
        query = session.query(Example).filter_by(snapshot_slug=snapshot_slug, example_kind=example_kind)

        if example_kind == ExampleKind.WHOLE_SNAPSHOT:
            if files_hash is not None:
                raise HTTPException(
                    status_code=400, detail=f"files_hash must be None for whole_snapshot examples, got: {files_hash}"
                )
            query = query.filter(Example.files_hash.is_(None))
        elif example_kind == ExampleKind.FILE_SET:
            if files_hash is None:
                raise HTTPException(status_code=400, detail="files_hash is required for file_set examples")
            query = query.filter_by(files_hash=files_hash)
        else:
            raise HTTPException(status_code=400, detail=f"Invalid example_kind: {example_kind}")

        example = query.first()
        if not example:
            raise HTTPException(
                status_code=404, detail=f"Example not found: {snapshot_slug}/{example_kind}/{files_hash or 'NULL'}"
            )

        # Get split from snapshot
        snapshot = session.query(Snapshot).filter_by(slug=snapshot_slug).first()
        if not snapshot:
            raise HTTPException(status_code=404, detail=f"Snapshot not found: {snapshot_slug}")

        split = snapshot.split

        # Get file list for file_set examples
        files: list[str] | None = None
        if example_kind == ExampleKind.FILE_SET and files_hash:
            file_members = (
                session.query(FileSetMember.file_path)
                .filter_by(snapshot_slug=snapshot_slug, files_hash=files_hash)
                .order_by(FileSetMember.file_path)
                .all()
            )
            files = [m.file_path for m in file_members]

        # Get per-definition stats from recall_by_definition_example view
        example_stats_rows = (
            session.query(RecallByDefinitionExample)
            .filter_by(snapshot_slug=snapshot_slug, example_kind=example_kind, files_hash=files_hash)
            .order_by(RecallByDefinitionExample.critic_image_digest)
            .all()
        )

        # Convert to DefinitionStatsForExample
        definitions = [
            DefinitionStatsForExample(
                image_digest=r.critic_image_digest,
                model=r.critic_model,
                n_runs=r.n_runs,
                status_counts=Counter(r.status_counts or {}),
                credit_stats=r.credit_stats,
            )
            for r in example_stats_rows
        ]

        # Compute aggregate stats across all definitions for this example
        credit_stats: StatsWithCI | None = None
        if definitions and any(d.credit_stats for d in definitions):
            # Aggregate credit_stats across all definitions
            all_credits = [d.credit_stats for d in definitions if d.credit_stats]
            if all_credits:
                # Simple mean aggregation (could be more sophisticated)
                total_n = sum(c.n for c in all_credits)
                if total_n > 0:
                    weighted_mean = sum(c.mean * c.n for c in all_credits) / total_n
                    all_mins = [c.min for c in all_credits]
                    all_maxs = [c.max for c in all_credits]
                    credit_stats = StatsWithCI(
                        n=total_n,
                        mean=weighted_mean,
                        min=min(all_mins),
                        max=max(all_maxs),
                        lcb95=None,  # Would need proper variance pooling
                        ucb95=None,
                    )

        return ExampleDetailResponse(
            snapshot_slug=snapshot_slug,
            example_kind=example_kind,
            files_hash=files_hash,
            split=split,
            recall_denominator=example.recall_denominator,
            files=files,
            definitions=definitions,
            credit_stats=credit_stats,
        )
