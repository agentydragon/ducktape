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
from sqlalchemy import Select, func, literal, select, union_all
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleKind, ExampleSpec, SingleFileSetExample, WholeSnapshotExample
from props.core.splits import Split
from props.db.models import AgentRun, FalsePositive, LLMRunCost, Snapshot, TpOccurrenceCredit, TruePositive


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
        >>> q = select(AgentRun).where(AgentRun.agent_run_id == bindparam('agent_run_id'))
        >>> compile_to_sql_with_placeholders(q)
        'SELECT ... WHERE agent_run_id = :agent_run_id'
    """
    return compile_to_sql(query, literal_binds=False)


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


def critic_dev_run_costs(critic_dev_run_id: UUID) -> Select:
    """Get per-run costs and totals for a critic developer run.

    Uses AgentRun with JSONB filtering to find all child runs (critics, graders)
    of a critic-dev agent run.

    Args:
        critic_dev_run_id: Critic developer agent run UUID (agent_run_id)

    Returns:
        Query selecting transcript details with cost/token metrics from llm_run_costs view
    """
    # CTE for critic-dev transcripts (all child agent runs + the critic-dev agent's own run)
    child_runs = select(
        AgentRun.agent_run_id,
        AgentRun.type_config["example"]["snapshot_slug"].astext.label("snapshot_slug"),
        AgentRun.type_config["agent_type"].astext.label("run_type"),
        AgentRun.created_at,
    ).where(AgentRun.parent_agent_run_id == critic_dev_run_id)

    # The critic-dev agent's own run
    critic_dev_agent_run = select(
        AgentRun.agent_run_id,
        literal(None).label("snapshot_slug"),
        literal("critic_dev_optimize").label("run_type"),
        AgentRun.created_at,
    ).where(AgentRun.agent_run_id == critic_dev_run_id)

    critic_dev_runs = union_all(child_runs, critic_dev_agent_run).cte("critic_dev_runs")

    # Main query joining with llm_run_costs view (LLM requests logged by proxy)
    return (
        select(
            critic_dev_runs.c.agent_run_id,
            critic_dev_runs.c.snapshot_slug,
            critic_dev_runs.c.run_type,
            LLMRunCost.model,
            func.sum(LLMRunCost.cost_usd).label("cost_usd"),
            func.sum(LLMRunCost.input_tokens).label("input_tokens"),
            func.sum(LLMRunCost.cached_input_tokens).label("cached_tokens"),
            func.sum(LLMRunCost.output_tokens).label("output_tokens"),
            critic_dev_runs.c.created_at,
        )
        .select_from(critic_dev_runs)
        .join(LLMRunCost, critic_dev_runs.c.agent_run_id == LLMRunCost.agent_run_id)
        .group_by(
            critic_dev_runs.c.agent_run_id,
            critic_dev_runs.c.snapshot_slug,
            critic_dev_runs.c.run_type,
            LLMRunCost.model,
            critic_dev_runs.c.created_at,
        )
        .order_by(critic_dev_runs.c.created_at.desc())
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

    Computes AVG(found_credit) from tp_occurrence_credits view, grouped by
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
    # Query TpOccurrenceCredit VIEW (uses example_kind + files_hash composite key)
    query = session.query(
        TpOccurrenceCredit.snapshot_slug,
        TpOccurrenceCredit.example_kind,
        TpOccurrenceCredit.files_hash,
        TpOccurrenceCredit.critic_image_digest,
        func.avg(TpOccurrenceCredit.found_credit).label("avg_credit_per_occurrence"),
    )

    if split is not None:
        query = query.filter(TpOccurrenceCredit.split == split)
    if critic_image_digest is not None:
        query = query.filter(TpOccurrenceCredit.critic_image_digest == critic_image_digest)
    if snapshot_slugs is not None:
        query = query.filter(TpOccurrenceCredit.snapshot_slug.in_(snapshot_slugs))

    query = query.group_by(
        TpOccurrenceCredit.snapshot_slug,
        TpOccurrenceCredit.example_kind,
        TpOccurrenceCredit.files_hash,
        TpOccurrenceCredit.critic_image_digest,
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
