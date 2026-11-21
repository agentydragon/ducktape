"""Run fixtures (critic runs, grader runs) for props tests.

Uses fake agent definitions with synthetic digests. These are DB-level test
helpers — they don't exercise the real OCI registry flow.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from props.core.agent_types import AgentType, CriticTypeConfig, GraderTypeConfig
from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleKind, ExampleSpec
from props.db.database import Database
from props.db.examples import Example
from props.db.models import (
    AgentDefinition,
    AgentRun,
    AgentRunStatus,
    FileSetMember,
    GradingEdge,
    LLMRequest,
    ReportedIssue,
    ReportedIssueOccurrence,
    Snapshot,
    TruePositiveOccurrenceORM,
)
from props.db.snapshots import LocationAnchor
from props.testing.fixtures.ground_truth import get_tp_occurrences_for_snapshot
from props.testing.models import BUDGET_TEST_MODEL, DEFAULT_TEST_MODEL

# Synthetic digests for DB-level tests (not real OCI images)
FAKE_CRITIC_DIGEST = "sha256:" + "0" * 64
FAKE_GRADER_DIGEST = "sha256:" + "1" * 64
FAKE_CRITIC_DEV_OPTIMIZE_DIGEST = "sha256:" + "2" * 64


def ensure_fake_agent_definitions(session: Session) -> None:
    """Insert fake agent_definitions rows if not already present.

    Idempotent — safe to call multiple times per session.
    """
    for digest, agent_type in [
        (FAKE_CRITIC_DIGEST, AgentType.CRITIC),
        (FAKE_GRADER_DIGEST, AgentType.GRADER),
        (FAKE_CRITIC_DEV_OPTIMIZE_DIGEST, AgentType.CRITIC_DEV_OPTIMIZE),
    ]:
        if session.query(AgentDefinition).filter_by(digest=digest).first() is None:
            session.add(AgentDefinition(digest=digest, agent_type=agent_type))
    session.flush()


def make_fake_critic_run(
    *,
    session: Session,
    example: ExampleSpec,
    model: str = DEFAULT_TEST_MODEL,
    status: AgentRunStatus = AgentRunStatus.EXITED,
    agent_run_id: UUID | None = None,
    parent_agent_run_id: UUID | None = None,
    budget_usd: float = 5.0,
) -> AgentRun:
    """Build AgentRun for critic with fake image digest.

    Automatically ensures fake agent_definitions rows exist.
    """
    ensure_fake_agent_definitions(session)

    if agent_run_id is None:
        agent_run_id = uuid4()

    type_config = CriticTypeConfig(example=example)

    return AgentRun(
        agent_run_id=agent_run_id,
        image_digest=FAKE_CRITIC_DIGEST,
        parent_agent_run_id=parent_agent_run_id,
        model=model,
        status=status,
        type_config=type_config,
        budget_usd=budget_usd,
    )


def make_llm_request(*, agent_run_id: UUID, input_tokens: int) -> LLMRequest:
    """Build LLMRequest that spends `input_tokens` uncached input tokens at `BUDGET_TEST_MODEL`'s known pricing."""
    return LLMRequest(
        agent_run_id=agent_run_id,
        model=BUDGET_TEST_MODEL,
        request_body={"model": BUDGET_TEST_MODEL},
        response_body={},
        input_tokens=input_tokens,
        cached_input_tokens=0,
        output_tokens=0,
        latency_ms=100,
    )


def make_fake_grader_run(
    *,
    session: Session,
    snapshot_slug: SnapshotSlug,
    model: str = DEFAULT_TEST_MODEL,
    status: AgentRunStatus = AgentRunStatus.EXITED,
) -> AgentRun:
    """Build AgentRun for snapshot grader with fake image digest.

    Automatically ensures fake agent_definitions rows exist.
    """
    ensure_fake_agent_definitions(session)

    type_config = GraderTypeConfig(snapshot_slug=snapshot_slug)

    return AgentRun(
        agent_run_id=uuid4(),
        image_digest=FAKE_GRADER_DIGEST,
        model=model,
        status=status,
        type_config=type_config,
        budget_usd=10_000.0,
    )


def overlap_file_for_occurrence(
    session: Session, snapshot_slug: SnapshotSlug, tp_id: str, occurrence_id: str, *, default: str = "subtract.py"
) -> str:
    """A reported-issue file that satisfies a TP occurrence's match_file_restriction.

    The grading-edge trigger (and the grading_pending view) only credit a critique
    against a restricted occurrence when the critique flagged a file overlapping the
    restriction set. Fixtures that match restricted occurrences must therefore report
    a file inside the restriction; for unrestricted occurrences any file works.
    """
    occ = session.get(TruePositiveOccurrenceORM, (snapshot_slug, tp_id, occurrence_id))
    assert occ is not None, f"No TP occurrence {tp_id}/{occurrence_id} for {snapshot_slug}"
    if occ.match_file_restriction is None:
        return default
    member = (
        session.query(FileSetMember)
        .filter_by(snapshot_slug=snapshot_slug, files_hash=occ.match_file_restriction)
        .order_by(FileSetMember.file_path)
        .first()
    )
    assert member is not None, f"Empty match_file_restriction file set for {tp_id}/{occurrence_id}"
    return member.file_path


def make_reported_issues(
    *, agent_run_id: UUID, issue_ids: list[str], session: Session, location_file: str = "subtract.py"
) -> list[ReportedIssue]:
    """Create ReportedIssue rows, each with one occurrence at `location_file`, for a critic run."""
    issues = []
    for issue_id in issue_ids:
        issue = ReportedIssue(agent_run_id=agent_run_id, issue_id=issue_id, rationale=f"Test issue {issue_id}")
        session.add(issue)
        session.flush()

        occurrence = ReportedIssueOccurrence(
            agent_run_id=agent_run_id,
            reported_issue_id=issue_id,
            locations=[LocationAnchor(file=location_file, start_line=1, end_line=1)],
        )
        session.add(occurrence)
        issues.append(issue)

    session.flush()
    return issues


def _add_credited_issue(
    *,
    session: Session,
    critic_run: AgentRun,
    grader_run: AgentRun,
    issue_id: str,
    tp_occurrence: tuple[str, str],
    credit: float,
) -> None:
    """Report `issue_id` for the critic run and add the grading edge crediting `tp_occurrence` with `credit`.

    The issue is reported at a file overlapping the occurrence's match_file_restriction, which
    the grading edge needs to be creditable (the trigger and the grading_pending view both
    require the overlap; see `overlap_file_for_occurrence`). The issue is flushed before the
    edge is added (FK constraint); the edge is flushed with the caller's next flush.
    """
    tp_id, occ_id = tp_occurrence
    snapshot_slug = critic_run.critic_config().example.snapshot_slug
    make_reported_issues(
        agent_run_id=critic_run.agent_run_id,
        issue_ids=[issue_id],
        session=session,
        location_file=overlap_file_for_occurrence(session, snapshot_slug, tp_id, occ_id),
    )
    session.add(
        GradingEdge(
            critique_run_id=critic_run.agent_run_id,
            critique_issue_id=issue_id,
            snapshot_slug=snapshot_slug,
            tp_id=tp_id,
            tp_occurrence_id=occ_id,
            credit=credit,
            rationale=f"Test (credit={credit})",
            grader_run_id=grader_run.agent_run_id,
        )
    )


def make_fake_critic_and_grader_run(
    *, example: ExampleSpec, tp_occurrences: list[tuple[str, str]], credit: float, session: Session
) -> tuple[AgentRun, AgentRun]:
    """One-stop helper: Creates complete critic+grader run with normalized tables."""
    critic_run = make_fake_critic_run(session=session, example=example, status=AgentRunStatus.EXITED)
    session.add(critic_run)
    session.flush()

    grader_run = make_fake_grader_run(
        session=session, snapshot_slug=example.snapshot_slug, model="test-grader", status=AgentRunStatus.EXITED
    )
    session.add(grader_run)
    session.flush()

    if credit > 0.0:
        for i, tp_occurrence in enumerate(tp_occurrences, start=1):
            _add_credited_issue(
                session=session,
                critic_run=critic_run,
                grader_run=grader_run,
                issue_id=f"issue-{i:03d}",
                tp_occurrence=tp_occurrence,
                credit=credit,
            )

    return critic_run, grader_run


def make_fake_grader_run_with_credit(
    *, session: Session, critic_run: AgentRun, tp_occurrence: tuple[str, str], credit: float, input_idx: int = 0
) -> AgentRun:
    """Create grader run + grading_edge for a critic run using real TP occurrence IDs."""
    grader_run = make_fake_grader_run(
        session=session, snapshot_slug=critic_run.critic_config().example.snapshot_slug, model="test-grader-model"
    )
    session.add(grader_run)
    session.flush()

    _add_credited_issue(
        session=session,
        critic_run=critic_run,
        grader_run=grader_run,
        issue_id=f"input-{input_idx}",
        tp_occurrence=tp_occurrence,
        credit=credit,
    )
    return grader_run


def _make_example_with_runs(slug: SnapshotSlug, credit: float, db: Database) -> tuple[ExampleSpec, AgentRun, AgentRun]:
    """Helper to create example with multiple critic and grader runs."""
    with db.session() as session:
        example = session.query(Example).filter_by(snapshot_slug=slug, example_kind=ExampleKind.WHOLE_SNAPSHOT).first()
        assert example, f"No whole-snapshot example found for {slug}"

        tp_occs = get_tp_occurrences_for_snapshot(slug, session)
        assert tp_occs, f"No TP occurrences found for {slug}"
        assert len(tp_occs) == example.recall_denominator, (
            f"Mismatch: {len(tp_occs)} TP occurrences vs {example.recall_denominator} expected"
        )

        example_spec = example.to_example_spec()
        critic_run, grader_run = make_fake_critic_and_grader_run(
            example=example_spec, tp_occurrences=tp_occs, credit=credit, session=session
        )

        # Create second pair for UCB/LCB computation which requires COUNT(*) > 1
        make_fake_critic_and_grader_run(
            example=example_spec, tp_occurrences=tp_occs, credit=credit * 0.9, session=session
        )

        session.commit()

        return (example_spec, critic_run, grader_run)


@pytest.fixture
def test_trivial_snapshot(synced_db: Database) -> Snapshot:
    """Provide the train1 snapshot (train split)."""
    with synced_db.session() as session:
        snapshot = session.query(Snapshot).filter_by(slug="test-fixtures/train1").one()
        session.expunge(snapshot)
        return snapshot


@pytest.fixture
def test_snapshot(synced_db: Database) -> SnapshotSlug:
    """Use test-fixtures/train1 snapshot from git fixtures."""
    return SnapshotSlug("test-fixtures/train1")


@pytest.fixture
def test_train_example_with_runs(synced_db: Database) -> tuple[ExampleSpec, AgentRun, AgentRun]:
    """Provide a train example with critic and grader runs."""
    return _make_example_with_runs(SnapshotSlug("test-fixtures/train1"), credit=0.8, db=synced_db)
