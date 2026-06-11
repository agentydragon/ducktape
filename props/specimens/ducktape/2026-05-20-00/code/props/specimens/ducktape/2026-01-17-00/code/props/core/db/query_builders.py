"""SQLAlchemy query builders for agent-accessible database queries.

Each function returns a SQLAlchemy Select object that can be:
- Executed directly in tests: session.execute(query).fetchall()
- Compiled to SQL string for j2 templates: compile_to_sql(query)

This provides a single source of truth for query structure, eliminating duplication
between test execution and template injection.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Select, bindparam, func, literal, select, union_all
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from props.core.agent_types import AgentType
from props.core.db.examples import Example
from props.core.db.models import (
    AgentRun,
    Event,
    FalsePositive,
    FalsePositiveOccurrenceORM,
    OccurrenceCredit,
    OccurrenceRangeORM,
    RunCost,
    Snapshot,
    TruePositive,
    TruePositiveOccurrenceORM,
)
from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleKind, ExampleSpec, SingleFileSetExample, WholeSnapshotExample
from props.core.splits import Split


def compile_to_sql(query: Select, *, literal_binds: bool = True) -> str:
    """Compile a SQLAlchemy Select to SQL string for template injection.

    Args:
        query: SQLAlchemy Select object
        literal_binds: If True, inline bound parameters as literals (for static SQL)
                      If False, use named placeholders like :param_name

    Returns:
        SQL string suitable for embedding in Jinja2 templates
    """
    compiled = query.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": literal_binds} if literal_binds else {}
    )
    return str(compiled)


def compile_to_sql_with_placeholders(query: Select) -> str:
    """Compile query to SQL with named parameter placeholders.

    Args:
        query: SQLAlchemy Select object with bound parameters

    Returns:
        SQL string with placeholders like :agent_run_id, :snapshot_slug

    Example:
        >>> q = select(Event).where(Event.agent_run_id == bindparam('agent_run_id'))
        >>> compile_to_sql_with_placeholders(q)
        'SELECT ... WHERE agent_run_id = :agent_run_id'
    """
    return compile_to_sql(query, literal_binds=False)


# ============================================================================
# Snapshot queries
# ============================================================================


# TODO: Consider removing - compiled but never rendered in template, agents can query directly
def list_train_snapshots() -> Select:
    """List all train split snapshots.

    Returns:
        Query selecting (slug, split) from train snapshots, ordered by slug
    """
    return select(Snapshot.slug, Snapshot.split).where(Snapshot.split == Split.TRAIN).order_by(Snapshot.slug)


# TODO: Consider removing - no usages found anywhere
def list_snapshots_by_split(split: str) -> Select:
    """List all snapshots for a given split.

    Args:
        split: One of 'train', 'valid', 'test'

    Returns:
        Query selecting (slug, split) from snapshots with given split, ordered by slug
    """
    return select(Snapshot.slug, Snapshot.split).where(Snapshot.split == split).order_by(Snapshot.slug)


# ============================================================================
# True positive / false positive queries
# ============================================================================


# TODO: Consider removing - no usages found anywhere
def list_true_positives_for_snapshot(snapshot_slug: SnapshotSlug) -> Select:
    """Get all true positives for a snapshot.

    Args:
        snapshot_slug: Snapshot slug to query

    Returns:
        Query selecting TruePositive ORM objects (access .occurrences via relationship)
    """
    return select(TruePositive).where(TruePositive.snapshot_slug == snapshot_slug).order_by(TruePositive.tp_id)


# TODO: Consider removing - no usages found anywhere
def list_false_positives_for_snapshot(snapshot_slug: SnapshotSlug) -> Select:
    """Get all false positives for a snapshot.

    Args:
        snapshot_slug: Snapshot slug to query

    Returns:
        Query selecting FalsePositive ORM objects (access .occurrences via relationship)
    """
    return select(FalsePositive).where(FalsePositive.snapshot_slug == snapshot_slug).order_by(FalsePositive.fp_id)


# TODO: Consider removing - compiled but never rendered in template, agents can query directly
def list_train_true_positives() -> Select:
    """List all true positives for train split snapshots.

    Returns:
        Query selecting (snapshot_slug, tp_id, rationale) for train snapshots
    """
    return (
        select(TruePositive.snapshot_slug, TruePositive.tp_id, TruePositive.rationale)
        .join(TruePositive.snapshot_obj)
        .where(Snapshot.split == Split.TRAIN)
        .order_by(TruePositive.snapshot_slug, TruePositive.tp_id)
    )


# TODO: Consider removing - compiled but never rendered in template, agents can query directly
def list_train_false_positives() -> Select:
    """List all false positives for train split snapshots.

    Returns:
        Query selecting (snapshot_slug, fp_id, rationale) for train snapshots
    """
    return (
        select(FalsePositive.snapshot_slug, FalsePositive.fp_id, FalsePositive.rationale)
        .join(FalsePositive.snapshot_obj)
        .where(Snapshot.split == Split.TRAIN)
        .order_by(FalsePositive.snapshot_slug, FalsePositive.fp_id)
    )


def count_issues_by_snapshot(split: str | None = None) -> Select:
    """Count true positives and false positives per snapshot.

    Args:
        split: Optional split filter ('train', 'valid', 'test')

    Returns:
        Query selecting (snapshot_slug, tp_count, fp_count)
    """
    # Subquery for TP counts
    tp_counts = (
        select(TruePositive.snapshot_slug, func.count().label("tp_count"))
        .group_by(TruePositive.snapshot_slug)
        .subquery()
    )

    # Subquery for FP counts
    fp_counts = (
        select(FalsePositive.snapshot_slug, func.count().label("fp_count"))
        .group_by(FalsePositive.snapshot_slug)
        .subquery()
    )

    # Main query joining snapshots with counts
    query = (
        select(
            Snapshot.slug.label("snapshot_slug"),
            func.coalesce(tp_counts.c.tp_count, 0).label("tp_count"),
            func.coalesce(fp_counts.c.fp_count, 0).label("fp_count"),
        )
        .outerjoin(tp_counts, Snapshot.slug == tp_counts.c.snapshot_slug)
        .outerjoin(fp_counts, Snapshot.slug == fp_counts.c.snapshot_slug)
        .order_by(Snapshot.slug)
    )

    if split is not None:
        query = query.where(Snapshot.split == split)

    return query


# ============================================================================
# Grader result queries
# ============================================================================


def snapshot_files_with_issues_select() -> Select:
    """Define the SELECT query for the snapshot_files_with_issues view.

    Computes the set of files with issues for each snapshot by joining with
    occurrence_ranges table to get all file paths.

    Replicates the logic of Snapshot.files_with_issues() method:
        tp_files = {file_path for tp in self.true_positives
                    for occurrence in tp.occurrences
                    for range in occurrence.ranges}
        fp_files = {file_path for fp in self.false_positives
                    for occurrence in fp.occurrences
                    for range in occurrence.ranges}
        return tp_files | fp_files

    RLS Note: This view inherits RLS from true_positives and false_positives tables,
    which may be filtered by temporary agent users (e.g., TRAIN-only for prompt optimizer).
    We also join with snapshots to ensure snapshot_slug is valid.

    Returns:
        Query selecting snapshot_slug and files_with_issues (text array) for each snapshot
    """
    # Extract all file paths from TP occurrence ranges
    # RLS on true_positives applies task-specific filtering for temporary agent users
    tp_files = (
        select(TruePositive.snapshot_slug, OccurrenceRangeORM.file_path.label("file_path"))
        .select_from(TruePositive)
        .join(TruePositiveOccurrenceORM, TruePositive.snapshot_slug == TruePositiveOccurrenceORM.snapshot_slug)
        .join(
            OccurrenceRangeORM,
            (TruePositive.snapshot_slug == OccurrenceRangeORM.snapshot_slug)
            & (TruePositive.tp_id == OccurrenceRangeORM.tp_id),
        )
    )

    # Extract all file paths from FP occurrence ranges
    # RLS on false_positives applies task-specific filtering for temporary agent users
    fp_files = (
        select(FalsePositive.snapshot_slug, OccurrenceRangeORM.file_path.label("file_path"))
        .select_from(FalsePositive)
        .join(FalsePositiveOccurrenceORM, FalsePositive.snapshot_slug == FalsePositiveOccurrenceORM.snapshot_slug)
        .join(
            OccurrenceRangeORM,
            (FalsePositive.snapshot_slug == OccurrenceRangeORM.snapshot_slug)
            & (FalsePositive.fp_id == OccurrenceRangeORM.fp_id),
        )
    )

    # Union and aggregate per snapshot
    all_files_union = union_all(tp_files, fp_files).subquery()

    # Join with snapshots to ensure snapshot_slug is valid and inherits snapshots RLS (no-op for train filter)
    return (
        select(
            all_files_union.c.snapshot_slug,
            func.array_agg(func.distinct(all_files_union.c.file_path)).label("files_with_issues"),
        )
        .select_from(all_files_union)
        .join(Snapshot, all_files_union.c.snapshot_slug == Snapshot.slug)
        .group_by(all_files_union.c.snapshot_slug)
    )


# ============================================================================
# Critic run queries
# ============================================================================


# TODO: Consider removing - no usages found (only parameterized version compiled)
def critic_runs_for_snapshot(snapshot_slug: SnapshotSlug, limit: int = 5) -> Select:
    """Get recent critic agent runs for a specific snapshot.

    Uses AgentRun with JSONB filtering on type_config->>'agent_type' = 'critic'
    and type_config->'example'->>'snapshot_slug' = snapshot_slug.

    Args:
        snapshot_slug: Snapshot to query
        limit: Maximum number of results (default 5)

    Returns:
        Query selecting agent run details for critic runs
    """
    return (
        select(
            AgentRun.agent_run_id,
            AgentRun.status,
            AgentRun.created_at,
            AgentRun.model,
            AgentRun.type_config["example"]["files_hash"].astext.label("files_hash"),
        )
        .where(
            AgentRun.type_config["agent_type"].astext == AgentType.CRITIC,
            AgentRun.type_config["example"]["snapshot_slug"].astext == snapshot_slug,
        )
        .order_by(AgentRun.created_at.desc())
        .limit(limit)
    )


# ============================================================================
# Parameterized query builders (for agent-side parameter substitution)
# ============================================================================


# TODO: Consider removing - compiled but never rendered in template
def critic_runs_for_snapshot_parameterized() -> Select:
    """Get critic runs for a snapshot (parameterized with :snapshot_slug placeholder).

    Agents fill in :snapshot_slug at runtime.
    """
    return critic_runs_for_snapshot(bindparam("snapshot_slug"), limit=5)  # type: ignore[arg-type]


def po_run_costs(po_run_id: UUID) -> Select:
    """Get per-run costs and totals for a prompt optimization run.

    Uses AgentRun with JSONB filtering to find all child runs (critics, graders)
    of a prompt optimizer agent run.

    Args:
        po_run_id: Prompt optimization agent run UUID (agent_run_id)

    Returns:
        Query selecting transcript details with cost/token metrics from run_costs view
    """
    # CTE for PO transcripts (all child agent runs + the PO agent's own run)
    # Child runs have parent_agent_run_id = po_run_id
    child_runs = select(
        AgentRun.agent_run_id,
        AgentRun.type_config["example"]["snapshot_slug"].astext.label("snapshot_slug"),
        AgentRun.type_config["agent_type"].astext.label("run_type"),
        AgentRun.created_at,
    ).where(AgentRun.parent_agent_run_id == po_run_id)

    # The PO agent's own run
    po_agent_run = select(
        AgentRun.agent_run_id,
        literal(None).label("snapshot_slug"),  # PO agent doesn't target a specific snapshot
        literal("prompt_optimizer").label("run_type"),
        AgentRun.created_at,
    ).where(AgentRun.agent_run_id == po_run_id)

    po_runs = union_all(child_runs, po_agent_run).cte("po_runs")

    # Main query joining with run_costs view (mapped as RunCost ORM model)
    # Note: RunCost.agent_run_id references agent_run_id
    return (
        select(
            po_runs.c.agent_run_id,
            po_runs.c.snapshot_slug,
            po_runs.c.run_type,
            RunCost.model,
            func.sum(RunCost.cost_usd).label("cost_usd"),
            func.sum(RunCost.input_tokens).label("input_tokens"),
            func.sum(RunCost.cached_tokens).label("cached_tokens"),
            func.sum(RunCost.output_tokens).label("output_tokens"),
            po_runs.c.created_at,
        )
        .select_from(po_runs)
        .join(RunCost, po_runs.c.agent_run_id == RunCost.agent_run_id)
        .group_by(
            po_runs.c.agent_run_id, po_runs.c.snapshot_slug, po_runs.c.run_type, RunCost.model, po_runs.c.created_at
        )
        .order_by(po_runs.c.created_at.desc())
    )


# TODO: Consider removing - no usages found (only non-param version used)
def po_run_costs_parameterized() -> Select:
    """PO run costs (parameterized with :po_run_id placeholder).

    Agents fill in :po_run_id at runtime.
    """
    return po_run_costs(bindparam("po_run_id"))  # type: ignore[arg-type]


# ============================================================================
# RLS blocked queries (examples showing what's blocked by RLS)
# ============================================================================


# TODO: Consider removing - compiled but never rendered in template
def blocked_valid_grader_runs() -> Select:
    """Example query that returns 0 rows due to RLS (valid split blocked).

    Uses AgentRun with JSONB filtering for grader agent type.
    Note: Graders derive snapshot_slug from the graded critic's type_config.

    Returns:
        Query attempting to select grader agent runs for valid split snapshots
    """
    # Grader runs: get snapshot_slug from the graded critic via a subquery
    # graded_agent_run_id -> lookup critic's type_config->'example'->>'snapshot_slug'
    graded_critic_snapshot = (
        select(AgentRun.type_config["example"]["snapshot_slug"].astext)
        .where(AgentRun.agent_run_id == func.cast(AgentRun.type_config["graded_agent_run_id"].astext, postgresql.UUID))
        .correlate(AgentRun)
        .scalar_subquery()
    )

    return select(AgentRun.agent_run_id, AgentRun.status).where(
        AgentRun.type_config["agent_type"].astext == AgentType.GRADER,
        graded_critic_snapshot.in_(select(Snapshot.slug).where(Snapshot.split == Split.VALID)),
    )


# TODO: Consider removing - compiled but never rendered in template
def blocked_valid_events() -> Select:
    """Example query that returns 0 rows due to RLS (valid split blocked).

    Uses AgentRun with JSONB filtering to find critic runs for valid split.

    Returns:
        Query attempting to count events for valid split critic agent runs
    """
    valid_agent_run_ids = (
        select(AgentRun.agent_run_id)
        .where(
            AgentRun.type_config["agent_type"].astext == AgentType.CRITIC,
            AgentRun.type_config["example"]["snapshot_slug"].astext.in_(
                select(Snapshot.slug).where(Snapshot.split == Split.VALID)
            ),
        )
        .scalar_subquery()
    )

    # Events reference agent_run_id
    return select(func.count()).select_from(Event).where(Event.agent_run_id.in_(valid_agent_run_ids))


# ============================================================================
# Scope queries
# ============================================================================


# TODO: Consider removing - compiled but never rendered in template
def list_train_scopes() -> Select:
    """List all examples for train split snapshots.

    Returns:
        Query selecting (snapshot_slug, example_kind, files_hash) for train snapshots
    """
    return (
        select(Example.snapshot_slug, Example.example_kind, Example.files_hash)
        .join(Snapshot, Example.snapshot_slug == Snapshot.slug)
        .where(Snapshot.split == Split.TRAIN)
        .order_by(Example.snapshot_slug, Example.example_kind, Example.files_hash)
    )


# ============================================================================
# Recall by Example Queries (Occurrence-Weighted)
# ============================================================================


class RecallByExampleRow(BaseModel):
    """Single row from recall-by-example query."""

    example: ExampleSpec
    critic_image_digest: str
    recall: float
    snapshot_slug: SnapshotSlug  # For backwards compatibility with existing code


def query_recall_by_example(
    session: Session,
    split: Split | None = None,
    critic_image_digest: str | None = None,
    snapshot_slugs: list[SnapshotSlug] | None = None,
) -> list[RecallByExampleRow]:
    """Query occurrence-weighted recall grouped by (example, critic_image_digest).

    Computes AVG(found_credit) from occurrence_credits view, grouped by
    (snapshot_slug, example_kind, files_hash, critic_image_digest).

    This is the canonical way to compute recall for cross-run aggregation.
    Single-run recall can be computed inline from occurrence_results.

    Args:
        session: SQLAlchemy session
        split: Optional split filter (TRAIN, VALID, TEST)
        critic_image_digest: Optional image digest filter (get recall for specific definition)
        snapshot_slugs: Optional list of snapshot slugs to filter

    Returns:
        List of RecallByExampleRow (example, critic_image_digest, recall)

    Example:
        # Get recall for all train examples with a specific definition
        results = query_recall_by_example(
            session,
            split=Split.TRAIN,
            critic_image_digest="sha256:abc123..."
        )
        for row in results:
            print(f"{row.example}: {row.recall * 100:.1f}%")
    """
    # Query OccurrenceCredit VIEW (uses example_kind + files_hash composite key)
    query = session.query(
        OccurrenceCredit.snapshot_slug,
        OccurrenceCredit.example_kind,
        OccurrenceCredit.files_hash,
        OccurrenceCredit.critic_image_digest,
        func.avg(OccurrenceCredit.found_credit).label("avg_credit_per_occurrence"),
    )

    if split is not None:
        query = query.filter(OccurrenceCredit.split == split)
    if critic_image_digest is not None:
        query = query.filter(OccurrenceCredit.critic_image_digest == critic_image_digest)
    if snapshot_slugs is not None:
        query = query.filter(OccurrenceCredit.snapshot_slug.in_(snapshot_slugs))

    query = query.group_by(
        OccurrenceCredit.snapshot_slug,
        OccurrenceCredit.example_kind,
        OccurrenceCredit.files_hash,
        OccurrenceCredit.critic_image_digest,
    )

    results = query.all()
    rows: list[RecallByExampleRow] = []
    for r in results:
        # Build ExampleSpec from query result
        if r.example_kind == ExampleKind.WHOLE_SNAPSHOT:
            example_spec: ExampleSpec = WholeSnapshotExample(snapshot_slug=r.snapshot_slug)
        elif r.example_kind == ExampleKind.FILE_SET:
            if r.files_hash is None:
                raise ValueError(f"example_kind=file_set but files_hash is NULL for {r.snapshot_slug}")
            example_spec = SingleFileSetExample(snapshot_slug=r.snapshot_slug, files_hash=r.files_hash)
        else:
            raise ValueError(f"Unknown example_kind: {r.example_kind}")

        rows.append(
            RecallByExampleRow(
                example=example_spec,
                critic_image_digest=r.critic_image_digest,
                recall=r.avg_credit_per_occurrence,
                snapshot_slug=r.snapshot_slug,
            )
        )
    return rows


# ============================================================================
# Aggregated Recall Views
# ============================================================================
# Query ORM models directly for recall stats:
#   - RecallByRun: per critic run
#   - RecallByDefinitionExample: per (definition, model, example)
#   - RecallByDefinitionSplitKind: per (definition, model, split, example_kind)
#   - RecallByExample: per (example, model)
#
# All views return: recall_denominator (denominator), credit_stats (numerator),
# recall_stats (credit_stats / recall_denominator).
