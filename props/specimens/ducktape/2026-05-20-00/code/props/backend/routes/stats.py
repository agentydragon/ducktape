"""Stats API routes for props dashboard.

Endpoints use agent credential passthrough - RLS policies filter results
based on the caller's database role. Useful for critic dev agents to see metrics.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from props.backend.auth import CallerDb
from props.backend.routes.ground_truth import get_snapshot_or_404
from props.core.agent_types import AgentType
from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleKind, ExampleSpec, SingleFileSetExample, WholeSnapshotExample
from props.core.splits import Split
from props.db.examples import Example, count_available_examples_by_scope_all
from props.db.models import (
    AgentDefinition,
    AgentRunStatus,
    FileSetMember,
    RecallByDefinitionExample,
    RecallByDefinitionSplitKind,
    StatsWithCI,
    TpOccurrenceCredit,
)
from props.db.query_builders import query_recall_by_example

router = APIRouter()


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
    display_name: str | None
    created_at: datetime
    stats: SplitStats


class OverviewResponse(BaseModel):
    definitions: list[DefinitionRow]
    example_counts: dict[Split, dict[ExampleKind, int]]


def to_split_scope_stats(row: RecallByDefinitionSplitKind, total_available: int) -> SplitScopeStats:
    return SplitScopeStats(
        recall_stats=row.recall_stats,
        n_examples=row.n_examples or 0,
        zero_count=row.zero_count or 0,
        status_counts=Counter(row.status_counts or {}),
        total_available=total_available,
    )


@router.get("/overview")
def get_overview(caller_db: CallerDb) -> OverviewResponse:
    with caller_db.session() as session:
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
            DefinitionRow(
                image_digest=d.digest, display_name=d.display_name, created_at=d.created_at, stats=build_stats(d.digest)
            )
            for d in all_definitions
        ]

        # Convert example_counts to nested dict
        nested_counts: dict[Split, dict[ExampleKind, int]] = defaultdict(dict)
        for (s, k), v in example_counts.items():
            nested_counts[s][k] = v

        return OverviewResponse(definitions=rows, example_counts=dict(nested_counts))


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
    display_name: str | None
    agent_type: AgentType
    created_at: datetime
    stats: SplitStats
    examples: list[ExampleStats]


@router.get("/definitions/{image_digest}")
def get_definition_detail(image_digest: str, caller_db: CallerDb) -> DefinitionDetailResponse:
    with caller_db.session() as session:
        definition = session.query(AgentDefinition).filter_by(digest=image_digest).first()
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
            display_name=definition.display_name,
            agent_type=AgentType(definition.agent_type),
            created_at=definition.created_at,
            stats=dict(stats),
            examples=examples,
        )


class DefinitionStatsForExample(BaseModel):
    image_digest: str
    model: str
    n_runs: int
    status_counts: dict[AgentRunStatus, int]
    credit_stats: StatsWithCI | None


class ExampleDetailResponse(BaseModel):
    snapshot_slug: SnapshotSlug
    example_kind: ExampleKind
    files_hash: str | None
    split: Split
    recall_denominator: int
    files: list[str] | None = Field(description="Resolved file paths for file_set examples")
    definitions: list[DefinitionStatsForExample] = Field(description="Per-definition stats")
    credit_stats: StatsWithCI | None = Field(description="Aggregate metrics across all definitions")


@router.get("/examples")
def get_example_detail(
    snapshot_slug: SnapshotSlug, example_kind: ExampleKind, caller_db: CallerDb, files_hash: str | None = None
) -> ExampleDetailResponse:
    with caller_db.session() as session:
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

        split = get_snapshot_or_404(session, snapshot_slug).split

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


# --- Occurrence stats ---


class OccurrenceStatsRow(BaseModel):
    snapshot_slug: SnapshotSlug
    split: Split
    tp_id: str
    occurrence_id: str
    n_runs: int
    mean_credit: float
    min_credit: float
    max_credit: float


class OccurrenceStatsResponse(BaseModel):
    occurrences: list[OccurrenceStatsRow]
    total: int


@router.get("/occurrences")
def get_occurrences(
    caller_db: CallerDb,
    snapshot_slug: SnapshotSlug | None = None,
    split: Split | None = None,
    limit: int = 100,
    sort_by: str = "mean_credit",
    sort_dir: str = "asc",
) -> OccurrenceStatsResponse:
    with caller_db.session() as session:
        query = session.query(
            TpOccurrenceCredit.snapshot_slug,
            TpOccurrenceCredit.split,
            TpOccurrenceCredit.tp_id,
            TpOccurrenceCredit.occurrence_id,
            func.count().label("n_runs"),
            func.avg(TpOccurrenceCredit.found_credit).label("mean_credit"),
            func.min(TpOccurrenceCredit.found_credit).label("min_credit"),
            func.max(TpOccurrenceCredit.found_credit).label("max_credit"),
        ).group_by(
            TpOccurrenceCredit.snapshot_slug,
            TpOccurrenceCredit.split,
            TpOccurrenceCredit.tp_id,
            TpOccurrenceCredit.occurrence_id,
        )

        if snapshot_slug is not None:
            query = query.filter(TpOccurrenceCredit.snapshot_slug == snapshot_slug)
        if split is not None:
            query = query.filter(TpOccurrenceCredit.split == split)

        total = query.count()

        sort_column = {"mean_credit": func.avg(TpOccurrenceCredit.found_credit), "n_runs": func.count()}.get(
            sort_by, func.avg(TpOccurrenceCredit.found_credit)
        )

        query = query.order_by(sort_column.desc()) if sort_dir == "desc" else query.order_by(sort_column.asc())

        results = query.limit(limit).all()

        occurrences = [
            OccurrenceStatsRow(
                snapshot_slug=r.snapshot_slug,
                split=r.split,
                tp_id=r.tp_id,
                occurrence_id=r.occurrence_id,
                n_runs=r.n_runs,
                mean_credit=float(r.mean_credit),
                min_credit=float(r.min_credit),
                max_credit=float(r.max_credit),
            )
            for r in results
        ]

        return OccurrenceStatsResponse(occurrences=occurrences, total=total)


# --- Coverage heatmap ---


class CoverageExample(BaseModel):
    snapshot_slug: SnapshotSlug
    example_kind: ExampleKind
    files_hash: str | None
    max_recall: float
    tp_count: int


class CoverageDefinition(BaseModel):
    image_digest: str
    best_on_count: int
    evaluated_on_count: int


class CoverageCell(BaseModel):
    definition_idx: int
    example_idx: int
    recall: float
    is_best: bool


class CoverageResponse(BaseModel):
    examples: list[CoverageExample]
    definitions: list[CoverageDefinition]
    cells: list[CoverageCell]
    max_recall_values: list[float]
    tp_count_values: list[int]


def _build_tp_counts_by_example(session: Session, split: Split) -> dict[ExampleSpec, int]:
    tp_count_results = (
        session.query(
            TpOccurrenceCredit.snapshot_slug,
            TpOccurrenceCredit.example_kind,
            TpOccurrenceCredit.files_hash,
            func.count(TpOccurrenceCredit.occurrence_id.distinct()).label("n_occurrences"),
        )
        .filter(TpOccurrenceCredit.split == split)
        .group_by(TpOccurrenceCredit.snapshot_slug, TpOccurrenceCredit.example_kind, TpOccurrenceCredit.files_hash)
        .all()
    )
    return {
        (
            WholeSnapshotExample(snapshot_slug=r.snapshot_slug)
            if r.example_kind == ExampleKind.WHOLE_SNAPSHOT
            else SingleFileSetExample(snapshot_slug=r.snapshot_slug, files_hash=r.files_hash)
        ): r.n_occurrences
        for r in tp_count_results
    }


@router.get("/coverage")
def get_coverage(split: Split, caller_db: CallerDb, limit_definitions: int = 15) -> CoverageResponse:
    with caller_db.session() as session:
        results = query_recall_by_example(session, split=split)

        # Group: example -> {definition_digest -> recall}
        by_example: dict[ExampleSpec, dict[str, float]] = defaultdict(dict)
        for row in results:
            by_example[row.example][row.critic_image_digest] = row.recall

        # Find best definitions per example, compute best_on_count
        best_on_count: Counter[str] = Counter()
        for recalls in by_example.values():
            if not recalls:
                continue
            max_recall = max(recalls.values())
            if max_recall > 0:
                for digest, recall in recalls.items():
                    if recall == max_recall:
                        best_on_count[digest] += 1

        # Sort definitions by best_on_count, take top N
        top_definitions = [d for d, _ in best_on_count.most_common(limit_definitions)]

        # Count how many examples each definition was evaluated on
        evaluated_on_count: Counter[str] = Counter()
        for recalls in by_example.values():
            evaluated_on_count.update(recalls.keys())

        # Only include examples that have nonzero max recall
        nonzero_examples = [ex for ex, recalls in by_example.items() if recalls and max(recalls.values()) > 0]

        # Get TP counts per example (used for heatmap and distribution histogram)
        tp_counts_by_example = _build_tp_counts_by_example(session, split)

        # Distribution histograms (over ALL examples, not just nonzero)
        max_recall_values = [max(recalls.values()) if recalls else 0.0 for recalls in by_example.values()]
        tp_count_values = list(tp_counts_by_example.values())

        # Build index maps
        def_to_idx = {d: i for i, d in enumerate(top_definitions)}
        ex_to_idx: dict[ExampleSpec, int] = {ex: i for i, ex in enumerate(nonzero_examples)}

        # Build response examples
        response_examples = [
            CoverageExample(
                snapshot_slug=ex.snapshot_slug,
                example_kind=ex.kind,
                files_hash=ex.files_hash if isinstance(ex, SingleFileSetExample) else None,
                max_recall=max(by_example[ex].values()),
                tp_count=tp_counts_by_example.get(ex, 0),
            )
            for ex in nonzero_examples
        ]

        # Build response definitions
        response_definitions = [
            CoverageDefinition(
                image_digest=d, best_on_count=best_on_count[d], evaluated_on_count=evaluated_on_count.get(d, 0)
            )
            for d in top_definitions
        ]

        # Build cells
        cells = []
        for ex, recalls in by_example.items():
            if ex not in ex_to_idx:
                continue
            max_recall = max(recalls.values())
            for digest, recall in recalls.items():
                if digest not in def_to_idx:
                    continue
                cells.append(
                    CoverageCell(
                        definition_idx=def_to_idx[digest],
                        example_idx=ex_to_idx[ex],
                        recall=recall,
                        is_best=(recall == max_recall),
                    )
                )

        return CoverageResponse(
            examples=response_examples,
            definitions=response_definitions,
            cells=cells,
            max_recall_values=max_recall_values,
            tp_count_values=tp_count_values,
        )
