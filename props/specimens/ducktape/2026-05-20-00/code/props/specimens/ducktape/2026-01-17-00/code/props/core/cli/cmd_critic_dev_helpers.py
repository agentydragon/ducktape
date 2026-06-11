"""Rollout analysis helpers for critic-dev commands.

Display functions for execution traces, run status, and grading summaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from rich.console import Console
from sqlalchemy import func, text

from agent_core.events import ApiRequest, AssistantText, Response, ToolCall, ToolCallOutput, UserText
from openai_utils.model import ReasoningItem
from props.core.agent_types import AgentType, CriticTypeConfig
from props.core.db.models import AgentRun, AgentRunStatus, Event, GradingEdge
from props.core.db.session import get_session
from props.core.display import ColumnDef, build_table_from_schema, ellipticize, print_table_with_footer, short_sha
from props.core.ids import SnapshotSlug


@dataclass
class CriticRunSummary:
    """Summary data for a critic run."""

    run_id: UUID
    snapshot_slug: SnapshotSlug
    image_digest: str
    status: str
    tool_count: int


def _fmt_event(event: Event) -> tuple[str, str] | None:
    """Format event payload for display. Returns (type, content) or None to skip."""
    p = event.payload
    if isinstance(p, ApiRequest | Response):
        return None
    if isinstance(p, ToolCall):
        return (event.event_type, f"{p.name} | {ellipticize(p.args_json or '{}', 100)}")
    if isinstance(p, ToolCallOutput):
        c = str(p.result.structuredContent or p.result.content or "").replace("\n", " ")
        return (f"{event.event_type} ({'ERROR' if p.result.isError else 'OK'})", ellipticize(c, 50))
    if isinstance(p, ReasoningItem):
        return (event.event_type, ellipticize(" | ".join(i.text for i in p.summary), 100))
    if isinstance(p, AssistantText | UserText):
        return (event.event_type, ellipticize(p.text, 100))
    return (event.event_type, ellipticize(str(p), 100))


def _get_descendant_run_ids(session, root_agent_run_id: UUID) -> list[UUID]:
    """Get all descendant agent run IDs (children, grandchildren, etc.) of a root agent."""
    cte_sql = text("""
        WITH RECURSIVE descendants AS (
            SELECT agent_run_id FROM agent_runs WHERE parent_agent_run_id = :root_id
            UNION ALL
            SELECT ar.agent_run_id
            FROM agent_runs ar
            JOIN descendants d ON ar.parent_agent_run_id = d.agent_run_id
        )
        SELECT agent_run_id FROM descendants
    """)
    result = session.execute(cte_sql, {"root_id": root_agent_run_id})
    return [row[0] for row in result]


def show_run_status(parent_agent_run_id: UUID | None = None) -> None:
    """Query run status statistics."""
    console = Console()
    with get_session() as session:
        descendant_ids: list[UUID] | None = None
        if parent_agent_run_id is not None:
            descendant_ids = _get_descendant_run_ids(session, parent_agent_run_id)
            if not descendant_ids:
                console.print("[yellow]No child runs found for this agent.[/yellow]")
                return
            console.print(f"[dim]Showing runs spawned by this agent ({len(descendant_ids)} total)[/dim]\n")

        cols: list[ColumnDef[Any, Any]] = [
            ColumnDef("Status", lambda r: r.status, width=30),
            ColumnDef("Count", lambda r: r.count, str, justify="right"),
        ]

        console.print("\n[bold]Critic Run Status:[/bold]")
        critic_q = session.query(
            AgentRun.status.label("status"), func.count(AgentRun.agent_run_id).label("count")
        ).filter(AgentRun.type_config["agent_type"].astext == AgentType.CRITIC)
        if descendant_ids is not None:
            critic_q = critic_q.filter(AgentRun.agent_run_id.in_(descendant_ids))
        critic_q = critic_q.group_by(AgentRun.status)
        console.print(build_table_from_schema(critic_q.all(), cols))

        console.print("\n[bold]Grader Run Status:[/bold]")
        grader_q = session.query(
            AgentRun.status.label("status"), func.count(AgentRun.agent_run_id).label("count")
        ).filter(AgentRun.type_config["agent_type"].astext == AgentType.GRADER)
        if descendant_ids is not None:
            grader_q = grader_q.filter(AgentRun.agent_run_id.in_(descendant_ids))
        grader_q = grader_q.group_by(AgentRun.status)
        console.print(build_table_from_schema(grader_q.all(), cols))

        console.print("\n[bold]Definitions with most max_turns_exceeded (top 5):[/bold]")
        mt = func.count().filter(AgentRun.status == AgentRunStatus.MAX_TURNS_EXCEEDED)
        pq = session.query(
            AgentRun.image_digest.label("image_digest"), mt.label("mt"), func.count().label("total")
        ).filter(AgentRun.type_config["agent_type"].astext == AgentType.CRITIC)
        if descendant_ids is not None:
            pq = pq.filter(AgentRun.agent_run_id.in_(descendant_ids))
        pq = pq.group_by(AgentRun.image_digest).order_by(mt.desc()).limit(5)
        pcols: list[ColumnDef[Any, Any]] = [
            ColumnDef("Definition", lambda r: r.image_digest, width=20),
            ColumnDef("MaxTurns", lambda r: r.mt, str, justify="right"),
            ColumnDef("Total", lambda r: r.total, str, justify="right"),
            ColumnDef(
                "Rate", lambda r: (r.mt / r.total * 100) if r.total > 0 else 0, lambda v: f"{v:.1f}%", justify="right"
            ),
        ]
        console.print(build_table_from_schema(pq.all(), pcols))


def show_execution_traces(limit: int = 5, parent_agent_run_id: UUID | None = None) -> None:
    """Show execution traces for recent critic runs."""
    console = Console()
    with get_session() as session:
        descendant_ids: list[UUID] | None = None
        if parent_agent_run_id is not None:
            descendant_ids = _get_descendant_run_ids(session, parent_agent_run_id)
            if not descendant_ids:
                console.print("[yellow]No child runs found for this agent.[/yellow]")
                return
            console.print(f"[dim]Showing runs spawned by this agent ({len(descendant_ids)} total)[/dim]\n")

        critic_q = session.query(AgentRun).filter(AgentRun.type_config["agent_type"].astext == AgentType.CRITIC)
        if descendant_ids is not None:
            critic_q = critic_q.filter(AgentRun.agent_run_id.in_(descendant_ids))
        critic_runs = critic_q.order_by(AgentRun.created_at.desc()).limit(limit).all()

        summaries = []
        for cr in critic_runs:
            if isinstance(cr.type_config, CriticTypeConfig):
                snapshot_slug = cr.type_config.example.snapshot_slug
            else:
                raise ValueError(f"Expected CriticTypeConfig, got {type(cr.type_config)}")
            tool_count = (
                session.query(Event)
                .filter(Event.agent_run_id == cr.agent_run_id, Event.event_type == "tool_call")
                .count()
            )
            summaries.append(
                CriticRunSummary(cr.agent_run_id, snapshot_slug, cr.image_digest, str(cr.status.value), tool_count)
            )

        console.print(f"\n[bold]Recent critic runs (last {limit}):[/bold]")
        cols: list[ColumnDef[Any, Any]] = [
            ColumnDef("Run", lambda r: str(r.run_id), short_sha, width=8),
            ColumnDef("Snapshot", lambda r: r.snapshot_slug, width=25),
            ColumnDef("Definition", lambda r: r.image_digest, width=20),
            ColumnDef("Status", lambda r: r.status, width=15),
            ColumnDef("Tools", lambda r: r.tool_count, str, justify="right", width=6),
        ]
        console.print(build_table_from_schema(summaries, cols))

        if summaries:
            s = summaries[0]
            console.print(f"\n[bold]Trace for {short_sha(str(s.run_id))}:[/bold]")
            cr_detail = session.get(AgentRun, s.run_id)
            if cr_detail:
                events = (
                    session.query(Event)
                    .filter(Event.agent_run_id == cr_detail.agent_run_id)
                    .order_by(Event.sequence_num)
                    .limit(100)
                    .all()
                )
                console.print(
                    f"Snapshot: {s.snapshot_slug} | Definition: {s.image_digest} | Status: {s.status} | Tools: {s.tool_count}\n"
                )
                rows = [(i, t, c) for i, (t, c) in enumerate(filter(None, (_fmt_event(e) for e in events)), 1)]
                ecols: list[ColumnDef[Any, Any]] = [
                    ColumnDef("#", lambda r: r[0], str, justify="right", width=3),
                    ColumnDef("Type", lambda r: r[1], width=12),
                    ColumnDef("Content", lambda r: r[2], width=80),
                ]
                total_events = session.query(Event).filter(Event.agent_run_id == cr_detail.agent_run_id).count()
                print_table_with_footer(
                    console, rows, ecols, show_header=True, total_count=total_events, item_name="events"
                )


def show_grading_summary(agent_run_id: UUID) -> None:
    """Show grading decision summary for a critic or grader run."""
    with get_session() as session:
        run = session.get(AgentRun, agent_run_id)
        if not run:
            print(f"Run not found: {agent_run_id}")
            return

        agent_type = run.type_config.agent_type

        if agent_type == AgentType.CRITIC:
            cr = run
            print(f"Critic: {short_sha(str(cr.agent_run_id))} | Definition: {cr.image_digest} | Model: {cr.model}")
            if cr.status != AgentRunStatus.COMPLETED:
                print(f"Status: {cr.status.value.upper()} (did not complete)")
                return

            gr = (
                session.query(AgentRun)
                .filter(
                    AgentRun.type_config["agent_type"].astext == AgentType.GRADER,
                    AgentRun.type_config["graded_agent_run_id"].astext == str(cr.agent_run_id),
                )
                .first()
            )
            if not gr:
                print("No grader result (not graded yet)")
                return
        elif agent_type == AgentType.GRADER:
            gr = run
        else:
            print(f"Expected critic or grader run, got: {agent_type}")
            return

        if gr.status != AgentRunStatus.COMPLETED:
            print(f"Grader status: {gr.status.value}")
            return

        grader_run_id = gr.agent_run_id
        print(f"Grader: {short_sha(str(grader_run_id))}\n")

        credit = (
            session.query(func.sum(GradingEdge.credit))
            .filter_by(grader_run_id=grader_run_id)
            .filter(GradingEdge.tp_id.isnot(None))
            .scalar()
            or 0.0
        )
        n_occ = (
            session.query(GradingEdge.tp_id, GradingEdge.tp_occurrence_id)
            .filter_by(grader_run_id=grader_run_id)
            .filter(GradingEdge.tp_id.isnot(None))
            .distinct()
            .count()
        )
        n_tps = (
            session.query(GradingEdge.tp_id)
            .filter_by(grader_run_id=grader_run_id)
            .filter(GradingEdge.tp_id.isnot(None))
            .distinct()
            .count()
        )
        n_novel = (
            session.query(GradingEdge)
            .filter_by(grader_run_id=grader_run_id)
            .filter(GradingEdge.tp_id.is_(None))
            .count()
        )
        print(f"Credit: {credit:.1f}/{n_occ} occ | {n_tps} TPs | {n_novel} unknown\n")

        missed_q = (
            session.query(GradingEdge)
            .filter_by(grader_run_id=grader_run_id)
            .filter(GradingEdge.tp_id.isnot(None), GradingEdge.credit == 0.0)
        )
        total_missed = missed_q.count()
        missed = missed_q.limit(5).all()
        if missed:
            print(f"Missed ({len(missed)}/{total_missed}):")
            for d in missed:
                print(f"  - {d.tp_id} occ {d.tp_occurrence_id}")
