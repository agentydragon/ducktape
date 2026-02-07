"""Run fixtures (critic runs, grader runs) for props tests.

Uses fake agent definitions with synthetic digests. These are DB-level test
helpers — they don't exercise the real OCI registry flow.
"""

from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy.orm import Session

from props.core.agent_types import AgentType, CriticTypeConfig, GraderTypeConfig
from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleKind, ExampleSpec
from props.core.models.types import Rationale
from props.db.database import Database
from props.db.examples import Example
from props.db.models import (
    AgentDefinition,
    AgentRun,
    AgentRunStatus,
    CanonicalIssuesSnapshot,
    GradingEdge,
    ReportedIssue,
    ReportedIssueOccurrence,
    Snapshot,
)
from props.db.snapshots import DBLocationAnchor
from props.testing.fixtures.ground_truth import get_tp_occurrences_for_snapshot

# Synthetic digests for DB-level tests (not real OCI images)
FAKE_CRITIC_DIGEST = "sha256:" + "0" * 64
FAKE_GRADER_DIGEST = "sha256:" + "1" * 64
FAKE_CRITIC_DEV_OPTIMIZE_DIGEST = "sha256:" + "2" * 64

# Props-specific constants
EMPTY_CANONICAL_ISSUES_SNAPSHOT = CanonicalIssuesSnapshot(true_positives=[], false_positives=[])


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
    model: str = "test-model",
    status: AgentRunStatus = AgentRunStatus.EXITED,
    agent_run_id: UUID | None = None,
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
        model=model,
        status=status,
        type_config=type_config,
        budget_usd=5.0,
    )


def make_fake_grader_run(
    *,
    session: Session,
    snapshot_slug: SnapshotSlug,
    model: str = "test-model",
    status: AgentRunStatus = AgentRunStatus.EXITED,
    agent_run_id: UUID | None = None,
) -> AgentRun:
    """Build AgentRun for daemon-based grader with fake image digest.

    Automatically ensures fake agent_definitions rows exist.
    """
    ensure_fake_agent_definitions(session)

    if agent_run_id is None:
        agent_run_id = uuid4()

    type_config = GraderTypeConfig(snapshot_slug=snapshot_slug)

    return AgentRun(
        agent_run_id=agent_run_id,
        image_digest=FAKE_GRADER_DIGEST,
        model=model,
        status=status,
        type_config=type_config,
        budget_usd=10_000.0,
    )


def make_reported_issues(
    *, agent_run_id: UUID, issue_ids: list[str], session: Session, location_file: str | None = "subtract.py"
) -> list[ReportedIssue]:
    """Create ReportedIssue rows (and optionally ReportedIssueOccurrence) for a critic run."""
    issues = []
    for issue_id in issue_ids:
        issue = ReportedIssue(agent_run_id=agent_run_id, issue_id=issue_id, rationale=f"Test issue {issue_id}")
        session.add(issue)
        session.flush()

        if location_file is not None:
            occurrence = ReportedIssueOccurrence(
                agent_run_id=agent_run_id,
                reported_issue_id=issue_id,
                locations=[DBLocationAnchor(file=location_file, start_line=1, end_line=1)],
            )
            session.add(occurrence)
        issues.append(issue)

    session.flush()
    return issues


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
        location_file = None if example.kind == ExampleKind.WHOLE_SNAPSHOT else "subtract.py"
        for i in range(1, len(tp_occurrences) + 1):
            issue_id = f"issue-{i:03d}"
            issue = ReportedIssue(agent_run_id=critic_run.agent_run_id, issue_id=issue_id, rationale=f"Test issue {i}")
            session.add(issue)
            if location_file:
                occ = ReportedIssueOccurrence(
                    agent_run_id=critic_run.agent_run_id,
                    reported_issue_id=issue_id,
                    locations=[DBLocationAnchor(file=location_file, start_line=1, end_line=1)],
                )
                session.add(occ)

        # Flush reported issues before adding grading edges (FK constraint)
        session.flush()

        for i, (tp_id, occ_id) in enumerate(tp_occurrences, start=1):
            issue_id = f"issue-{i:03d}"
            edge = GradingEdge(
                critique_run_id=critic_run.agent_run_id,
                critique_issue_id=issue_id,
                snapshot_slug=example.snapshot_slug,
                tp_id=tp_id,
                tp_occurrence_id=occ_id,
                fp_id=None,
                fp_occurrence_id=None,
                credit=credit,
                rationale=f"Test (credit={credit})",
                grader_run_id=grader_run.agent_run_id,
            )
            session.add(edge)

    return critic_run, grader_run


def make_fake_grader_run_with_credit(
    *,
    session: Session,
    critic_run: AgentRun,
    tp_occurrence: tuple[str, str],
    credit: float,
    input_idx: int = 0,
    model: str = "test-grader-model",
) -> AgentRun:
    """Create grader run + grading_edge for a critic run using real TP occurrence IDs."""
    tp_id, occ_id = tp_occurrence
    issue_id = f"input-{input_idx}"

    issue = ReportedIssue(agent_run_id=critic_run.agent_run_id, issue_id=issue_id, rationale=f"Test issue {input_idx}")
    session.add(issue)
    occ = ReportedIssueOccurrence(
        agent_run_id=critic_run.agent_run_id,
        reported_issue_id=issue_id,
        locations=[DBLocationAnchor(file="subtract.py", start_line=1, end_line=1)],
    )
    session.add(occ)

    snapshot_slug = critic_run.critic_config().example.snapshot_slug
    grader_run = make_fake_grader_run(session=session, snapshot_slug=snapshot_slug, model=model)
    session.add(grader_run)
    session.flush()

    edge = GradingEdge(
        critique_run_id=critic_run.agent_run_id,
        critique_issue_id=issue_id,
        snapshot_slug=snapshot_slug,
        tp_id=tp_id,
        tp_occurrence_id=occ_id,
        fp_id=None,
        fp_occurrence_id=None,
        credit=credit,
        rationale=f"Credit {credit}",
        grader_run_id=grader_run.agent_run_id,
    )
    session.add(edge)

    return grader_run


def make_critic_run_with_issues(db: Database, example: ExampleSpec, num_issues: int = 1) -> UUID:
    """Create a committed critic run with ReportedIssue rows.

    Returns the critic_run_id.
    """
    with db.session() as session:
        critic_run = make_fake_critic_run(session=session, example=example, status=AgentRunStatus.EXITED)
        session.add(critic_run)
        session.flush()

        for i in range(1, num_issues + 1):
            issue_id = f"input-{i:03d}"
            session.add(
                ReportedIssue(
                    agent_run_id=critic_run.agent_run_id, issue_id=issue_id, rationale=f"Test input issue {i}"
                )
            )

        session.commit()
        return critic_run.agent_run_id


def make_grader_run_for_critic(
    db: Database, critic_run_id: UUID, status: AgentRunStatus = AgentRunStatus.EXITED
) -> UUID:
    """Create a grader run for a given critic run, deriving the snapshot from the critic config."""
    with db.session() as session:
        critic_run = session.query(AgentRun).filter_by(agent_run_id=critic_run_id).one()
        snapshot_slug = critic_run.critic_config().example.snapshot_slug

        grader_run = make_fake_grader_run(session=session, snapshot_slug=snapshot_slug, status=status)
        session.add(grader_run)
        session.commit()
        return grader_run.agent_run_id


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
def rationale_model() -> type[BaseModel]:
    """Fixture providing a Pydantic model with Rationale field."""

    class Model(BaseModel):
        rationale: Rationale

    return Model


@pytest.fixture
def test_trivial_snapshot(synced_db: Database) -> Snapshot:
    """Provide the train1 snapshot (train split)."""
    with synced_db.session() as session:
        snapshot = session.query(Snapshot).filter_by(slug="test-fixtures/train1").one()
        session.expunge(snapshot)
        return snapshot


@pytest.fixture
def test_validation_snapshot(synced_db: Database) -> Snapshot:
    """Provide the valid1 snapshot (valid split)."""
    with synced_db.session() as session:
        snapshot = session.query(Snapshot).filter_by(slug="test-fixtures/valid1").one()
        session.expunge(snapshot)
        return snapshot


@pytest.fixture
def test_snapshot(synced_db: Database) -> SnapshotSlug:
    """Use test-fixtures/train1 snapshot from git fixtures."""
    return SnapshotSlug("test-fixtures/train1")


@pytest.fixture
def test_validation_snapshot_slug(synced_db: Database) -> SnapshotSlug:
    """Return test-validation fixture snapshot slug."""
    return SnapshotSlug("test-fixtures/valid1")


@pytest.fixture
def test_train_example_with_runs(synced_db: Database) -> tuple[ExampleSpec, AgentRun, AgentRun]:
    """Provide a train example with critic and grader runs."""
    return _make_example_with_runs(SnapshotSlug("test-fixtures/train1"), credit=0.8, db=synced_db)


@pytest.fixture
def test_valid_example_with_runs(synced_db: Database) -> tuple[ExampleSpec, AgentRun, AgentRun]:
    """Provide a valid example with critic and grader runs."""
    return _make_example_with_runs(SnapshotSlug("test-fixtures/valid1"), credit=0.6, db=synced_db)
