"""Grader agent CLI for matching critique issues to ground truth.

The grader's job is to fill in all edges in the bipartite graph between critique
issues and matchable GT occurrences. Each edge represents your judgment about whether
a critique issue matches a GT occurrence.

Workflow:
1. `list pending` - See what edges are missing
2. `show issue/gt` - Inspect issues and GT occurrences
3. `match` - Create edges with credit and rationale
4. `fill` - Bulk-fill remaining edges for an issue
5. `submit` - Finalize when all edges are complete
"""

from __future__ import annotations

import re
from typing import Annotated
from uuid import UUID

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from props.core.display import short_uuid
from props.db.models import (
    FalsePositive,
    FalsePositiveOccurrenceORM,
    GradingPending,
    ReportedIssue,
    ReportedIssueOccurrence,
    TruePositive,
    TruePositiveOccurrenceORM,
)
from props.db.session import get_session
from props.grader.edge_helpers import delete_edges_for_issue, get_pending_edges, insert_edge

HELP_TEXT = """Grader agent CLI for matching critique issues to ground truth.

Commands for viewing pending work, creating grading edges, and submitting.

WORKFLOW:

  1. Check pending work:
     props grader-agent list pending
     props grader-agent list pending --run <critic-run-id>  # snapshot mode

  2. Inspect an issue or GT occurrence:
     props grader-agent show issue <issue-id> [--run <critic-run-id>]
     props grader-agent show gt tp/<tp-id>/<occ-id>
     props grader-agent show gt fp/<fp-id>/<occ-id>

  3. Create edges (match issue to occurrences):
     props grader-agent match <issue-id> "rationale" tp/<id>/<occ>:1.0 [--run <critic-run-id>]

  4. Bulk-fill remaining edges for an issue:
     props grader-agent fill <issue-id> <expected-count> "rationale" [--run <critic-run-id>]

  5. Finalize:
     props grader-agent submit "summary"

EDGE MODEL:

  Every (critique_issue, matchable_occurrence) pair needs an edge.
  - TP edges: credit 0.0-1.0 (how well issue matches)
  - FP edges: credit 0.0 (not triggered) or >0 (incorrectly triggered)

  No separate "no-match" type - use credit=0.0 for non-matches.

SNAPSHOT MODE:

  In snapshot mode (grading multiple critic runs), use --run to specify
  which critic run's issues you're working with. The `list pending` output
  shows critic run IDs to help identify which run each issue belongs to.
"""

app = typer.Typer(name="grader-agent", help=HELP_TEXT, add_completion=False)
list_app = typer.Typer(help="List resources")
show_app = typer.Typer(help="Show details of a resource")
app.add_typer(list_app, name="list")
app.add_typer(show_app, name="show")


def _parse_run_id(run: str | None) -> UUID | None:
    if run is None:
        return None
    # If it looks like a full UUID, parse directly
    if len(run) == 36 and run.count("-") == 4:
        return UUID(run)
    # Otherwise treat as short UUID prefix - need to resolve from pending edges
    pending = get_pending_edges()
    for p in pending:
        if str(p.critique_run_id).startswith(run):
            return p.critique_run_id
    raise typer.BadParameter(f"No critic run found matching prefix: {run}")


@list_app.command("pending")
def list_pending_cmd(
    issue: Annotated[str | None, typer.Option("--issue", "-i", help="Filter to specific issue")] = None,
    gt: Annotated[str | None, typer.Option("--gt", "-g", help="Filter to specific GT occurrence")] = None,
    run: Annotated[
        str | None, typer.Option("--run", "-r", help="Filter to specific critic run (full or short UUID)")
    ] = None,
) -> None:
    """List pending (missing) grading edges.

    Shows critique issues and GT occurrences that still need edges.

    Examples:
        props grader-agent list pending
        props grader-agent list pending --issue input-001
        props grader-agent list pending --run a1b2c3d4
    """
    critique_run_id = _parse_run_id(run) if run else None
    pending = get_pending_edges(issue_filter=issue, gt_filter=gt, critique_run_id=critique_run_id)

    if not pending:
        typer.echo("No pending edges - grading complete.")
        return

    # Check if we have multiple critic runs (snapshot mode)
    run_ids = {p.critique_run_id for p in pending}
    is_snapshot_mode = len(run_ids) > 1

    # Group by (critic_run, issue)
    by_run_issue: dict[tuple[UUID, str], list[GradingPending]] = {}
    for p in pending:
        key = (p.critique_run_id, p.critique_issue_id)
        by_run_issue.setdefault(key, []).append(p)

    console = Console()
    table = Table(box=box.SIMPLE, show_edge=False)

    if is_snapshot_mode:
        table.add_column("Run", style="dim")
    table.add_column("Issue")
    table.add_column("Edges", justify="right")
    table.add_column("GT Occurrences", style="dim")

    for (run_id, issue_id), edges in sorted(by_run_issue.items(), key=lambda x: (str(x[0][0]), x[0][1])):
        occs = []
        for p in edges:
            if p.tp_id:
                occs.append(f"tp/{p.tp_id}/{p.tp_occurrence_id}")
            else:
                occs.append(f"fp/{p.fp_id}/{p.fp_occurrence_id}")

        occ_preview = ", ".join(sorted(occs)[:3])
        if len(occs) > 3:
            occ_preview += f" (+{len(occs) - 3})"

        if is_snapshot_mode:
            table.add_row(short_uuid(run_id), issue_id, str(len(occs)), occ_preview)
        else:
            table.add_row(issue_id, str(len(occs)), occ_preview)

    console.print(table)


@show_app.command("issue")
def show_issue_cmd(
    issue_id: Annotated[str, typer.Argument(help="Issue ID to show")],
    run: Annotated[str | None, typer.Option("--run", "-r", help="Critic run ID (required in snapshot mode)")] = None,
) -> None:
    """Show details of a critique issue.

    Example:
        props grader-agent show issue input-001
        props grader-agent show issue input-001 --run a1b2c3d4
    """
    critique_run_id = _parse_run_id(run) if run else None

    with get_session() as session:
        query = session.query(ReportedIssue).filter_by(issue_id=issue_id)
        if critique_run_id:
            query = query.filter_by(agent_run_id=critique_run_id)

        issue = query.first()
        if not issue:
            typer.echo(f"Issue not found: {issue_id}", err=True)
            raise typer.Exit(1)

        typer.echo(f"Issue: {issue.issue_id} (run: {short_uuid(issue.agent_run_id)})")
        typer.echo(
            f"Rationale: {issue.rationale[:200]}..." if len(issue.rationale) > 200 else f"Rationale: {issue.rationale}"
        )

        occs = (
            session.query(ReportedIssueOccurrence)
            .filter_by(agent_run_id=issue.agent_run_id, reported_issue_id=issue.issue_id)
            .all()
        )
        if occs:
            typer.echo("Locations:")
            for occ in occs:
                for loc in occ.locations or []:
                    typer.echo(f"  - {loc.file}:{loc.start_line or '?'}-{loc.end_line or '?'}")


@show_app.command("gt")
def show_gt_cmd(gt_ref: Annotated[str, typer.Argument(help="GT reference: tp/<id>/<occ> or fp/<id>/<occ>")]) -> None:
    """Show details of a ground truth occurrence.

    Examples:
        props grader-agent show gt tp/security-issue/occ-0
        props grader-agent show gt fp/dup-pattern/occ-0
    """
    parts = gt_ref.split("/")
    if len(parts) != 3 or parts[0] not in ("tp", "fp"):
        typer.echo(f"Invalid GT reference: {gt_ref}. Use tp/<id>/<occ> or fp/<id>/<occ>", err=True)
        raise typer.Exit(1)

    gt_type, gt_id, occ_id = parts

    with get_session() as session:
        if gt_type == "tp":
            tp = session.query(TruePositive).filter_by(tp_id=gt_id).first()
            if not tp:
                typer.echo(f"TP not found: {gt_id}", err=True)
                raise typer.Exit(1)

            occ = session.query(TruePositiveOccurrenceORM).filter_by(tp_id=gt_id, occurrence_id=occ_id).first()
            if not occ:
                typer.echo(f"TP occurrence not found: {gt_id}/{occ_id}", err=True)
                raise typer.Exit(1)

            typer.echo(f"TP: {gt_id}/{occ_id}")
            typer.echo(
                f"Rationale: {tp.rationale[:300]}..." if len(tp.rationale) > 300 else f"Rationale: {tp.rationale}"
            )
            files_dict = {str(r.file_path): (r.start_line, r.end_line) for r in occ.ranges}
            typer.echo(f"Files: {files_dict}")
            if occ.note:
                typer.echo(f"Note: {occ.note}")
        else:
            fp = session.query(FalsePositive).filter_by(fp_id=gt_id).first()
            if not fp:
                typer.echo(f"FP not found: {gt_id}", err=True)
                raise typer.Exit(1)

            fp_occ = session.query(FalsePositiveOccurrenceORM).filter_by(fp_id=gt_id, occurrence_id=occ_id).first()
            if not fp_occ:
                typer.echo(f"FP occurrence not found: {gt_id}/{occ_id}", err=True)
                raise typer.Exit(1)

            typer.echo(f"FP: {gt_id}/{occ_id}")
            typer.echo(
                f"Rationale: {fp.rationale[:300]}..." if len(fp.rationale) > 300 else f"Rationale: {fp.rationale}"
            )
            files_dict = {str(r.file_path): (r.start_line, r.end_line) for r in fp_occ.ranges}
            typer.echo(f"Files: {files_dict}")
            if fp_occ.note:
                typer.echo(f"Note: {fp_occ.note}")


@app.command("match")
def match_cmd(
    issue_id: Annotated[str, typer.Argument(help="Critique issue ID")],
    rationale: Annotated[str, typer.Argument(help="Explanation for the matches")],
    edges: Annotated[list[str], typer.Argument(help="Edges: tp/<id>/<occ>:<credit> or fp/<id>/<occ>:<credit>")],
    run: Annotated[str | None, typer.Option("--run", "-r", help="Critic run ID (required in snapshot mode)")] = None,
) -> None:
    """Create grading edges matching an issue to GT occurrences.

    Each edge specifies a GT occurrence and credit value.

    Examples:
        props grader-agent match input-001 "Exact security match" tp/security-issue/occ-0:1.0

        props grader-agent match input-002 "Reviewed all, only dead-code matches" \\
            tp/dead-code/occ-0:1.0 tp/other-issue/occ-0:0 fp/known-pattern/occ-0:0
    """
    critique_run_id = _parse_run_id(run) if run else None
    edge_pattern = re.compile(r"^(tp|fp)/([^/]+)/([^:]+):([0-9.]+)$")

    created = 0
    for edge_spec in edges:
        m = edge_pattern.match(edge_spec)
        if not m:
            typer.echo(
                f"Invalid edge format: {edge_spec}. Use tp/<id>/<occ>:<credit> or fp/<id>/<occ>:<credit>", err=True
            )
            raise typer.Exit(1)

        gt_type, gt_id, occ_id, credit_str = m.groups()
        credit = float(credit_str)

        if gt_type == "tp":
            insert_edge(
                critique_issue_id=issue_id,
                tp_id=gt_id,
                tp_occurrence_id=occ_id,
                credit=credit,
                rationale=rationale,
                critique_run_id=critique_run_id,
            )
        else:
            insert_edge(
                critique_issue_id=issue_id,
                fp_id=gt_id,
                fp_occurrence_id=occ_id,
                credit=credit,
                rationale=rationale,
                critique_run_id=critique_run_id,
            )
        created += 1

    remaining = get_pending_edges(issue_filter=issue_id, critique_run_id=critique_run_id)
    if remaining:
        typer.echo(f"Created {created} edges. {len(remaining)} remaining for this issue.")
    else:
        typer.echo(f"Created {created} edges. Issue complete.")


@app.command("fill")
def fill_cmd(
    issue_id: Annotated[str, typer.Argument(help="Critique issue ID")],
    expected_count: Annotated[int, typer.Argument(help="Expected number of remaining edges (safety check)")],
    rationale: Annotated[str, typer.Argument(help="Explanation for the fill")],
    run: Annotated[str | None, typer.Option("--run", "-r", help="Critic run ID (required in snapshot mode)")] = None,
) -> None:
    """Bulk-fill remaining edges for an issue with credit=0.0.

    Use after setting high-credit matches to fill remaining non-matches.
    The expected_count is a safety check against GT drift.

    Example:
        props grader-agent fill input-001 5 "Reviewed, no matches for remaining"
    """
    critique_run_id = _parse_run_id(run) if run else None
    remaining = get_pending_edges(issue_filter=issue_id, critique_run_id=critique_run_id)

    if len(remaining) != expected_count:
        typer.echo(
            f"Error: Expected {expected_count} remaining edges, found {len(remaining)}. "
            "GT may have changed. Re-check with 'list pending'.",
            err=True,
        )
        raise typer.Exit(1)

    for p in remaining:
        if p.tp_id:
            insert_edge(
                critique_issue_id=issue_id,
                tp_id=p.tp_id,
                tp_occurrence_id=p.tp_occurrence_id,
                credit=0.0,
                rationale=rationale,
                critique_run_id=critique_run_id,
            )
        else:
            insert_edge(
                critique_issue_id=issue_id,
                fp_id=p.fp_id,
                fp_occurrence_id=p.fp_occurrence_id,
                credit=0.0,
                rationale=rationale,
                critique_run_id=critique_run_id,
            )

    typer.echo(f"Filled {len(remaining)} edges with credit=0.0. Issue complete.")


@app.command("delete")
def delete_cmd(
    issue_id: Annotated[str, typer.Argument(help="Critique issue ID whose edges to delete")],
    run: Annotated[str | None, typer.Option("--run", "-r", help="Critic run ID (required in snapshot mode)")] = None,
) -> None:
    """Delete all grading edges for an issue (to redo).

    Example:
        props grader-agent delete input-002
    """
    critique_run_id = _parse_run_id(run) if run else None
    count = delete_edges_for_issue(issue_id, critique_run_id=critique_run_id)
    typer.echo(f"Deleted {count} edges for {issue_id}")
