"""Shared test fixtures for props tests."""

import hashlib
import inspect
import os
from collections.abc import AsyncGenerator, Callable, Generator
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from agent_core_testing.openai_mock import FakeOpenAIModel
from agent_core_testing.responses import DecoratorMock
from agent_core_testing.steps import Step
from mcp_infra.exec.models import BaseExecResult
from openai_utils.model import FunctionCallItem, ResponsesRequest, ResponsesResult
from props.core.agent_registry import AgentRegistry
from props.core.agent_types import CriticTypeConfig, GraderTypeConfig
from props.core.agent_workspace import WorkspaceManager
from props.core.db.agent_definition_ids import CRITIC_AGENT_DEFINITION_ID, GRADER_AGENT_DEFINITION_ID
from props.core.db.config import DatabaseConfig, get_database_config
from props.core.db.examples import Example
from props.core.db.models import (
    AgentRun,
    AgentRunStatus,
    CanonicalIssuesSnapshot,
    FalsePositiveOccurrenceORM,
    FileSet,
    FileSetMember,
    GradingEdge,
    ReportedIssue,
    ReportedIssueOccurrence,
    Snapshot,
    TruePositiveOccurrenceORM,
)
from props.core.db.session import dispose_db, get_session, init_db, recreate_database
from props.core.db.setup import ensure_database_exists
from props.core.db.snapshots import DBLocationAnchor
from props.core.db.sync.sync import sync_all
from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleKind, ExampleSpec, SingleFileSetExample, WholeSnapshotExample
from props.core.models.true_positive import FalsePositiveOccurrence, LineRange, TruePositiveOccurrence
from props.core.prompt_improve.improve_agent import run_improvement_agent
from props.core.prompt_improve.reminder_handler import TerminationSuccess
from props.core.prompt_optimize.prompt_optimizer import run_prompt_optimizer
from props.core.prompt_optimize.target_metric import TargetMetric
from props.core.rationale import Rationale

# Register shared fixtures from other packages
pytest_plugins = [
    "agent_core_testing.fixtures",  # Recording handler, make_test_agent, etc.
    "agent_core_testing.responses",  # make_step_runner, responses_factory, etc.
    "mcp_infra.testing.fixtures",  # async_docker_client, make_compositor, etc.
]


class PropsMock(DecoratorMock):
    """Mock with props-specific helpers (psql, etc.)."""

    def psql_roundtrip(
        self, query: str, *, timeout_ms: int = 5000
    ) -> Generator[FunctionCallItem, ResponsesRequest, BaseExecResult]:
        """Execute psql query via docker exec and return result."""
        return self.docker_exec_roundtrip(["psql", "-c", query], timeout_ms=timeout_ms)


# Props-specific constants
EMPTY_CANONICAL_ISSUES_SNAPSHOT = CanonicalIssuesSnapshot(true_positives=[], false_positives=[])
TEST_FIXTURES_PATH = Path(__file__).parent / "fixtures" / "specimens"


@pytest.fixture
def test_specimens_base() -> Path:
    """Path to test specimens directory (git-tracked fixtures)."""
    return TEST_FIXTURES_PATH


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add custom pytest command-line options."""
    parser.addoption(
        "--keep-db",
        action="store_true",
        default=False,
        help="Preserve test database on failure for debugging (does not drop test database after test)",
    )


@pytest.fixture
def test_workspace_manager(tmp_path: Path) -> WorkspaceManager:
    """Shared WorkspaceManager fixture for agent tests.

    Creates a WorkspaceManager rooted in the test's tmp_path directory.
    This is preferred over creating WorkspaceManager directly in tests.
    """
    return WorkspaceManager(tmp_path)


# NOTE: Use Example.from_spec(session, spec) directly to look up examples.
# Example is generated from ground truth during sync - from_spec just retrieves it.


@pytest.fixture(autouse=True)
def block_production_config_in_tests(monkeypatch: pytest.MonkeyPatch) -> Callable:
    """Prevent test functions from accidentally using production database.

    Tests should use the test_db fixture, which creates isolated test databases.
    Calling get_database_config() from test code is a bug - it returns production
    database credentials instead of the test-specific isolated database.

    This fixture blocks ALL calls to get_database_config() from test files.
    Production code (like database session management, Alembic offline mode) can
    still call it normally.
    """

    original = get_database_config

    def _block_from_tests(*args, **kwargs):
        # Check the immediate caller (frame 1)
        stack = inspect.stack()
        if len(stack) > 1:
            caller_frame = stack[1]
            caller_file = caller_frame.filename
            # If called from a test file, fail
            if "/tests/" in caller_file and caller_file.endswith(".py"):
                raise RuntimeError(
                    f"Tests must use test_db fixture, not get_database_config()!\n"
                    f"Called from: {caller_file}:{caller_frame.lineno}\n"
                    f"Fix: Use 'config = test_db' instead of 'get_database_config()'."
                )
        # Called from production code - allow it
        return original(*args, **kwargs)

    monkeypatch.setattr("props_core.db.config.get_database_config", _block_from_tests)

    # Return original for test_db fixture to use
    return original


def get_tp_occurrences_for_snapshot(snapshot_slug: str, session: Session) -> list[tuple[str, str]]:
    """Get all TP occurrence (tp_id, occurrence_id) tuples for a snapshot.

    Args:
        snapshot_slug: The snapshot slug to query
        session: Database session

    Returns:
        List of (tp_id, occurrence_id) tuples

    Example:
        with get_session() as session:
            tp_occs = get_tp_occurrences_for_snapshot("test-fixtures/train1", session)
            output = make_grader_output(tp_occurrences=tp_occs, found_credit=0.8)
    """
    rows = (
        session.query(TruePositiveOccurrenceORM.tp_id, TruePositiveOccurrenceORM.occurrence_id)
        .filter_by(snapshot_slug=snapshot_slug)
        .order_by(TruePositiveOccurrenceORM.tp_id, TruePositiveOccurrenceORM.occurrence_id)
        .all()
    )
    return [(row.tp_id, row.occurrence_id) for row in rows]


# ============================================================================
# Test Fixture Builders (Pydantic models, not dicts!)
# ============================================================================


def make_tp_occurrence(
    occurrence_id: str = "occ-1",
    files: dict[Path, list[LineRange] | None] | None = None,
    critic_scopes_expected_to_recall: set[frozenset[Path]] | None = None,
    note: str | None = None,
) -> TruePositiveOccurrence:
    """Build TruePositiveOccurrence with proper Pydantic types.

    Args:
        occurrence_id: Unique ID within the TP (default: "occ-1")
        files: File paths with optional line ranges
            - None (default): Single file Path("test.py") with no ranges
            - {Path("file.py"): None}: File with no line ranges
            - {Path("file.py"): [LineRange(...)]}: File with line ranges
        critic_scopes_expected_to_recall: Minimal file sets for detection
            - None (default): Single trigger set containing first file
            - {frozenset([Path("file.py")])}: Set of frozensets of Paths
        note: Occurrence-specific note (optional)

    Returns:
        TruePositiveOccurrence with validated Pydantic types

    Examples:
        # Simple file, no line range (most common):
        make_tp_occurrence(files={Path("test.py"): None})

        # File with line range:
        make_tp_occurrence(
            files={Path("server.py"): [LineRange(start_line=10, end_line=20)]}
        )

        # Multiple trigger sets (OR logic):
        make_tp_occurrence(
            critic_scopes_expected_to_recall={frozenset([Path("file1.py")]), frozenset([Path("file2.py")])}
        )

        # Trigger set requiring multiple files (AND logic):
        make_tp_occurrence(
            critic_scopes_expected_to_recall={frozenset([Path("client.py"), Path("utils.py")])}
        )
    """
    # Default: single file with no ranges
    if files is None:
        files = {Path("test.py"): None}

    # Default: single trigger set containing first file
    if critic_scopes_expected_to_recall is None:
        first_file = next(iter(files.keys()))
        critic_scopes_expected_to_recall = {frozenset([first_file])}

    return TruePositiveOccurrence(
        occurrence_id=occurrence_id,
        files=files,
        note=note,
        critic_scopes_expected_to_recall=critic_scopes_expected_to_recall,
    )


def make_fp_occurrence(
    occurrence_id: str = "occ-1",
    files: dict[Path, list[LineRange] | None] | None = None,
    relevant_files: set[Path] | None = None,
    note: str | None = None,
) -> FalsePositiveOccurrence:
    """Build FalsePositiveOccurrence with proper Pydantic types.

    Args:
        occurrence_id: Unique ID within the FP (default: "occ-1")
        files: File paths with optional line ranges (same format as make_tp_occurrence)
        relevant_files: Files that make this FP relevant
            - None (default): First file from files dict
            - {Path("file.py")}: Set of Paths
        note: Occurrence-specific note (optional)

    Returns:
        FalsePositiveOccurrence with validated Pydantic types

    Examples:
        # Simple FP:
        make_fp_occurrence(
            files={Path("helper.py"): None},
            relevant_files={Path("helper.py")}
        )
    """
    # Default: single file with no ranges
    if files is None:
        files = {Path("test.py"): None}

    # Default: first file from files dict
    if relevant_files is None:
        first_file = next(iter(files.keys()))
        relevant_files = {first_file}

    return FalsePositiveOccurrence(occurrence_id=occurrence_id, files=files, note=note, relevant_files=relevant_files)


# ============================================================================
# TruePositive / FalsePositive Builders
# ============================================================================


# ============================================================================
# Other Model Builders
# ============================================================================


# make_critique is DEPRECATED - Critique table has been eliminated
# Use make_critic_run instead to create AgentRun records for critic runs


# DEPRECATED: get_example() and get_or_create_example() removed
# Examples are now database VIEWs auto-generated from file_sets and snapshots tables
# Query directly:
#   example = session.query(Example).filter_by(
#       snapshot_slug="test-fixtures/train1",
#       scope_kind=ExampleKind.WHOLE_SNAPSHOT
#   ).one()
#
# Or for single-file-set:
#   example = session.query(Example).filter_by(
#       snapshot_slug="test-fixtures/train1",
#       scope_kind=ExampleKind.FILE_SET,
#       files_hash=123
#   ).one()


def make_critic_run(
    *,  # Force keyword arguments
    example: Example,  # Required, not optional
    model: str = "test-model",
    status: AgentRunStatus = AgentRunStatus.COMPLETED,
    completion_summary: str | None = None,
    agent_run_id: UUID | None = None,
    image_digest: str = CRITIC_AGENT_DEFINITION_ID,
) -> AgentRun:
    """Build AgentRun for critic from Example (preferred pattern).

    Derives snapshot_slug and ExampleSpec from example automatically.

    Args:
        example: Example ORM object to derive ExampleSpec from (required)
        model: Model name (default: "test-model")
        status: Run status (default: COMPLETED)
        completion_summary: Markdown summary (auto-provided for COMPLETED status if None)
        agent_run_id: Optional agent run ID (defaults to uuid4())
        image_digest: Image digest (default: CRITIC_AGENT_DEFINITION_ID)

    Returns:
        AgentRun ORM model (not yet added to session)

    Examples:
        # Basic usage:
        make_critic_run(example=my_example)

        # With specific status:
        make_critic_run(example=my_example, status=AgentRunStatus.MAX_TURNS_EXCEEDED)

        # With explicit agent_run_id (for tests that need specific IDs):
        make_critic_run(example=my_example, agent_run_id=my_uuid)
    """
    # agent_run_id defaults to uuid4()
    if agent_run_id is None:
        agent_run_id = uuid4()

    # Auto-provide completion_summary for COMPLETED status (required by CHECK constraint)
    if completion_summary is None and status == AgentRunStatus.COMPLETED:
        completion_summary = "Test completion summary"

    # Convert Example ORM to ExampleSpec Pydantic model
    example_spec = example.to_example_spec()

    # Build type_config using Pydantic model directly
    type_config = CriticTypeConfig(example=example_spec)

    return AgentRun(
        agent_run_id=agent_run_id,
        image_digest=image_digest,
        model=model,
        status=status,
        completion_summary=completion_summary,
        type_config=type_config,
    )


def make_grader_run(
    *,  # Force keyword arguments
    critic_run: AgentRun,  # Required
    canonical_issues_snapshot=EMPTY_CANONICAL_ISSUES_SNAPSHOT,
    model: str = "test-model",
    status: AgentRunStatus = AgentRunStatus.COMPLETED,
    agent_run_id: UUID | None = None,
    image_digest: str = GRADER_AGENT_DEFINITION_ID,
) -> AgentRun:
    """Build AgentRun for grader from critic AgentRun (derives graded_agent_run_id).

    Args:
        critic_run: Critic run being evaluated (derives graded_agent_run_id)
        canonical_issues_snapshot: Snapshot of TPs+FPs used (default: EMPTY_CANONICAL_ISSUES_SNAPSHOT)
        model: Model name (default: "test-model")
        status: Run status (default: COMPLETED)
        agent_run_id: Optional agent run ID (defaults to uuid4())
        image_digest: Image digest (default: GRADER_AGENT_DEFINITION_ID)

    Returns:
        AgentRun ORM model (not yet added to session)

    Examples:
        # Minimal usage (with critic_run):
        make_grader_run(critic_run=my_critic_run)

        # With specific status:
        make_grader_run(critic_run=my_critic_run, status=AgentRunStatus.MAX_TURNS_EXCEEDED)

        # With custom canonical issues:
        make_grader_run(critic_run=my_critic_run, canonical_issues_snapshot=my_snapshot)
    """
    if agent_run_id is None:
        agent_run_id = uuid4()

    # Build type_config using Pydantic model directly
    type_config = GraderTypeConfig(
        graded_agent_run_id=critic_run.agent_run_id,
        canonical_issues_snapshot=canonical_issues_snapshot.model_dump(mode="json"),
    )

    return AgentRun(
        agent_run_id=agent_run_id, image_digest=image_digest, model=model, status=status, type_config=type_config
    )


def make_reported_issues(
    *, agent_run_id: UUID, issue_ids: list[str], session: Session, location_file: str | None = "subtract.py"
) -> list[ReportedIssue]:
    """Create ReportedIssue rows (and optionally ReportedIssueOccurrence) for a critic run.

    Deterministic factory - always creates fresh issues, no conditional logic.
    Call once per critic run with all issue IDs upfront.

    Args:
        agent_run_id: The agent run (critic) these issues belong to
        issue_ids: List of issue IDs to create (e.g., ["input-1", "input-2"])
        session: Database session
        location_file: File path for occurrence locations. Default "subtract.py" exists in train1 fixture.
            Pass None to skip occurrence creation (useful for whole_snapshot examples).

    Returns:
        List of created ReportedIssue objects
    """
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


def make_critic_and_grader_run(
    *, example: Example, tp_occurrences: list[tuple[str, str]], credit: float, session: Session
) -> tuple[AgentRun, AgentRun]:
    """One-stop helper: Creates complete critic+grader run with normalized tables.

    Creates:
    - Critic AgentRun with COMPLETED status
    - ReportedIssue rows (one per TP occurrence if credit > 0)
    - ReportedIssueOccurrence rows (placeholder locations)
    - Grader AgentRun with COMPLETED status
    - GradingEdge rows linking issues to TP occurrences

    Args:
        example: Example being evaluated
        tp_occurrences: List of (tp_id, occurrence_id) tuples
        credit: Credit for each edge (0.0 = no edges created)
        session: Database session

    Returns:
        (critic_run, grader_run) tuple of AgentRun objects
    """
    # Create critic run
    critic_run = make_critic_run(example=example, status=AgentRunStatus.COMPLETED)
    session.add(critic_run)
    session.flush()

    # Create grader run
    grader_run = make_grader_run(
        critic_run=critic_run,
        canonical_issues_snapshot=EMPTY_CANONICAL_ISSUES_SNAPSHOT,
        model="test-grader",
        status=AgentRunStatus.COMPLETED,
    )
    session.add(grader_run)
    session.flush()

    # Create edges with generated issue IDs (if credit > 0)
    if credit > 0.0:
        location_file = None if example.example_kind == ExampleKind.WHOLE_SNAPSHOT else "subtract.py"
        for i, (tp_id, occ_id) in enumerate(tp_occurrences, start=1):
            issue_id = f"issue-{i:03d}"
            # Create reported issue
            issue = ReportedIssue(agent_run_id=critic_run.agent_run_id, issue_id=issue_id, rationale=f"Test issue {i}")
            session.add(issue)
            if location_file:
                occ = ReportedIssueOccurrence(
                    agent_run_id=critic_run.agent_run_id,
                    reported_issue_id=issue_id,
                    locations=[DBLocationAnchor(file=location_file, start_line=1, end_line=1)],
                )
                session.add(occ)
            # Create grading edge
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


@pytest.fixture
def rationale_model() -> type[BaseModel]:
    """Fixture providing a Pydantic model with Rationale field."""

    class Model(BaseModel):
        rationale: Rationale

    return Model


# =============================================================================
# Run managers fixtures
# =============================================================================


@pytest.fixture
def mock_snapshot_slug() -> SnapshotSlug:
    """Shared test snapshot slug."""
    return SnapshotSlug("ducktape/2025-11-26-00")


@pytest.fixture
def noop_openai_client() -> FakeOpenAIModel:
    """Mock OpenAI client with no responses - for unused critic/grader clients."""
    return FakeOpenAIModel(outputs=[])


@pytest.fixture
def make_openai_client() -> Callable[[list[ResponsesResult]], FakeOpenAIModel]:
    """Factory fixture for creating mock OpenAI clients from response sequences.

    Usage:
        responses = [factory.make(...), factory.make(...)]
        client = make_openai_client(responses)

    This is a props-specific alias for the pattern used in agent tests (make_fake_openai).
    """

    def _factory(responses: list[ResponsesResult]) -> FakeOpenAIModel:
        return FakeOpenAIModel(responses)

    return _factory


def _sanitize_test_id(test_id: str, max_length: int = 63) -> str:
    """Sanitize pytest node ID for use in PostgreSQL database name.

    Args:
        test_id: pytest node ID (e.g., 'tests/props/test_db.py::test_sync')
        max_length: Maximum length for PostgreSQL identifier (default 63)

    Returns:
        Sanitized database name safe for PostgreSQL
    """
    # Keep only alphanumeric and underscore; replace other chars with underscore
    sanitized = "".join(c if c.isalnum() or c == "_" else "_" for c in test_id)
    # Collapse consecutive underscores
    while "__" in sanitized:
        sanitized = sanitized.replace("__", "_")
    # Trim leading/trailing underscores
    sanitized = sanitized.strip("_")
    # Ensure it fits PostgreSQL's 63-character limit (including 'props_test_' prefix)
    # Reserve space for the prefix that will be added later
    prefix = "props_test_"
    available_length = max_length - len(prefix)
    if len(sanitized) > available_length:
        # Keep prefix and add hash suffix to ensure uniqueness
        hash_suffix = hashlib.sha256(test_id.encode()).hexdigest()[:8]
        prefix_length = available_length - len(hash_suffix) - 1
        sanitized = f"{sanitized[:prefix_length]}_{hash_suffix}"
    return sanitized


@pytest.fixture
def test_db(
    request: pytest.FixtureRequest, block_production_config_in_tests: Callable
) -> Generator[DatabaseConfig, None, None]:
    """Create isolated database for each test.

    Creates a unique database per test, initializes schema, and drops it after.
    Safe for parallel pytest-xdist execution - each test gets its own database.

    Database name is derived from the test node ID for better debuggability.

    Yields:
        DatabaseConfig for the test database (with both admin and agent credentials)
    """
    # Generate database name from test node ID
    test_node_id = request.node.nodeid
    sanitized_id = _sanitize_test_id(test_node_id)
    db_name = f"props_test_{sanitized_id}"

    # Get base config (uses the original function, bypassing the mock)
    get_database_config_original = block_production_config_in_tests
    base_config = get_database_config_original()  # Uses env vars set by devenv

    # Create test database (idempotent - drops existing if present)
    ensure_database_exists(base_config, db_name, drop_existing=True)

    # Build config for the new test database
    test_config = base_config.with_database(db_name)

    # Keep postgres engine for teardown
    postgres_config = base_config.with_database("postgres")
    postgres_engine = create_engine(postgres_config.admin_url(), isolation_level="AUTOCOMMIT")

    # Dispose any existing connection (needed for per-test isolation in parallel tests)

    dispose_db()

    # Initialize schema in the new database
    init_db(test_config)
    recreate_database()

    yield test_config  # Test runs here with access to config

    # Cleanup: drop the test database (unless --keep-db option or KEEP_TEST_DB env var is set)
    keep_db = request.config.getoption("--keep-db") or os.environ.get("KEEP_TEST_DB") == "1"
    if keep_db:
        print(f"\n\n=== KEEPING TEST DATABASE: {db_name} ===")
        print(f"Database config: {test_config}")
        print(f"Connect with: direnv exec adgn psql -d {db_name}")
        postgres_engine.dispose()
        return

    with postgres_engine.connect() as conn:
        # Terminate connections to the test database
        conn.execute(
            text(
                f"""
            SELECT pg_terminate_backend(pg_stat_activity.pid)
            FROM pg_stat_activity
            WHERE pg_stat_activity.datname = '{db_name}'
              AND pid <> pg_backend_pid()
        """
            )
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))

    postgres_engine.dispose()


@pytest.fixture
def admin_engine(test_db: DatabaseConfig) -> Generator:
    """Create admin engine for test database with proper disposal.

    Use this instead of manually creating engines in tests.
    Automatically disposes the engine after the test completes.

    Args:
        test_db: Test database configuration fixture

    Yields:
        SQLAlchemy Engine configured with admin credentials for the test database
    """

    engine = create_engine(test_db.admin_url())
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def test_snapshot(synced_test_db: DatabaseConfig) -> SnapshotSlug:
    """Use test-fixtures/train1 snapshot from git fixtures.

    Returns test-fixtures/train1 slug which is already synced by synced_test_db.
    Uses function-scoped synced_test_db to support tests that write to the database.

    Returns:
        SnapshotSlug for the git fixture snapshot
    """
    # Use git fixture snapshot (already synced via synced_test_db)
    return SnapshotSlug("test-fixtures/train1")


# =============================================================================
# Shared syncing helper and session-scoped fixtures
# =============================================================================


def _sync_test_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sync test fixtures to the current database.

    Shared helper used by both synced_test_db and _session_synced_db.
    Sets ADGN_PROPS_SPECIMENS_ROOT to test fixtures path and runs sync_all().
    """
    monkeypatch.setenv("ADGN_PROPS_SPECIMENS_ROOT", str(TEST_FIXTURES_PATH))
    with get_session() as session:
        sync_all(session, use_staged=True)


@pytest.fixture(scope="session")
def session_monkeypatch() -> Generator[pytest.MonkeyPatch, None, None]:
    """Session-scoped monkeypatch for environment variable overrides.

    pytest doesn't provide session-scoped monkeypatch, so we create one.
    """
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _session_synced_db(
    request: pytest.FixtureRequest, session_monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[DatabaseConfig, None]:
    """Internal: Session-scoped synced database.

    Use synced_readonly_session instead of this directly.
    Creates a single shared database for all read-only tests in the session.
    """
    # Create session-scoped test database with fixed name
    db_name = "props_test_session_shared"
    base_config = get_database_config()
    ensure_database_exists(base_config, db_name, drop_existing=True)
    test_config = base_config.with_database(db_name)

    # Keep postgres engine for teardown
    postgres_config = base_config.with_database("postgres")
    postgres_engine = create_engine(postgres_config.admin_url(), isolation_level="AUTOCOMMIT")

    dispose_db()
    init_db(test_config)
    recreate_database()

    # Sync test fixtures
    _sync_test_fixtures(session_monkeypatch)

    yield test_config

    # Cleanup at end of session
    with postgres_engine.connect() as conn:
        conn.execute(
            text(
                f"""
            SELECT pg_terminate_backend(pg_stat_activity.pid)
            FROM pg_stat_activity
            WHERE pg_stat_activity.datname = '{db_name}'
              AND pid <> pg_backend_pid()
        """
            )
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    postgres_engine.dispose()


@pytest.fixture(scope="session")
def synced_readonly_session(_session_synced_db: DatabaseConfig) -> Generator[Session, None, None]:
    """Session-scoped SQLAlchemy Session for READ-ONLY tests.

    Eliminates `with get_session() as session:` boilerplate for read-only tests.
    The database is synced once per session and shared across all tests using this fixture.

    WARNING: Do not commit/write via this session - use synced_test_db for write tests.

    Usage:
        def test_query_examples(synced_readonly_session: Session):
            examples = synced_readonly_session.query(Example).all()
            assert len(examples) > 0
    """
    with get_session() as session:
        yield session


@pytest.fixture
def synced_test_db(test_db: DatabaseConfig, monkeypatch: pytest.MonkeyPatch) -> DatabaseConfig:
    """Test database with test fixture specimens synced (not production).

    Syncs test-trivial and test-validation from tests/props/fixtures/specimens/.
    These are git-tracked fixtures with known issues for faster, hermetic testing.

    Uses production CLI sync code via environment override.
    """
    _sync_test_fixtures(monkeypatch)
    return test_db


@pytest.fixture
def synced_test_session(synced_test_db: DatabaseConfig) -> Generator[Session, None, None]:
    """Function-scoped session over synced test database (read-write).

    Use for tests that need to write to the database.
    Eliminates `with get_session() as session:` boilerplate.

    For read-only tests, prefer synced_readonly_session (session-scoped, faster).
    """
    with get_session() as session:
        yield session


@pytest.fixture
def test_validation_snapshot_slug(synced_test_db: DatabaseConfig) -> SnapshotSlug:
    """Return test-validation fixture snapshot slug (after syncing test fixtures).

    Test fixture snapshot with issues (TPs/FPs) for validation.
    Lives in tests/props/fixtures/specimens/test-fixtures/valid1/.
    """
    return SnapshotSlug("test-fixtures/valid1")


# =============================================================================
# ExampleSpec Fixtures
# =============================================================================


@pytest.fixture
def all_files_scope(test_snapshot: SnapshotSlug) -> WholeSnapshotExample:
    """WholeSnapshotExample for test-trivial fixture.

    Use this when testing whole-snapshot evaluation paths.
    Returns a frozen Pydantic model that can be used as dict key.
    """
    return WholeSnapshotExample(snapshot_slug=test_snapshot)


@pytest.fixture
def subtract_file_example(synced_test_db: DatabaseConfig) -> SingleFileSetExample:
    """SingleFileSetExample for subtract.py in train1.

    Queries the database to get the actual files_hash for the subtract.py scope.
    The file set is created during sync from ground truth critic_scopes_expected_to_recall.
    """
    slug = SnapshotSlug("test-fixtures/train1")
    with get_session() as session:
        # Find file set that has exactly "subtract.py" file
        # by finding FileSetMember with subtract.py and checking it's the only file
        fs = (
            session.query(FileSet)
            .join(FileSetMember)
            .filter(FileSet.snapshot_slug == slug)
            .filter(FileSetMember.file_path == "subtract.py")
            .first()
        )
        assert fs is not None, "No file set found for subtract.py in train1"
        return SingleFileSetExample(snapshot_slug=slug, files_hash=fs.files_hash)


# ---------------------------------------------------------------------------
# Shared ORM fixtures for real TP/FP occurrences from git fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def example_subtract_orm(synced_test_session: Session) -> Example:
    """ORM Example for subtract.py (single TP occurrence) from git-synced fixture."""
    slug = SnapshotSlug("test-fixtures/train1")
    example = (
        synced_test_session.query(Example)
        .filter_by(snapshot_slug=slug)
        .filter(Example.files_hash.isnot(None))
        .filter(Example.recall_denominator == 1)
        .first()
    )
    assert example is not None, "Expected single-file-set example with 1 TP in expected recall scope in train1"
    return example


@pytest.fixture
def example_multi_tp_orm(synced_test_session: Session) -> Example:
    """ORM Example with multiple TP occurrences from git-synced fixture (e.g., add.py)."""
    slug = SnapshotSlug("test-fixtures/train1")
    example = (
        synced_test_session.query(Example)
        .filter_by(snapshot_slug=slug)
        .filter(Example.files_hash.isnot(None))
        .filter(Example.recall_denominator > 1)
        .first()
    )
    assert example is not None, "Expected file-set example with multiple TP occurrences in train1"
    return example


@pytest.fixture
def tp_occurrence_single(synced_test_session: Session, example_subtract_orm: Example) -> tuple[str, str]:
    """(tp_id, occurrence_id) for a real single-occurrence TP in train1."""
    tp_occ = (
        synced_test_session.query(TruePositiveOccurrenceORM)
        .filter_by(snapshot_slug=example_subtract_orm.snapshot_slug)
        .order_by(TruePositiveOccurrenceORM.tp_id, TruePositiveOccurrenceORM.occurrence_id)
        .first()
    )
    assert tp_occ is not None, "Expected at least one TP occurrence in train1"
    return tp_occ.tp_id, tp_occ.occurrence_id


@pytest.fixture
def tp_single_id(tp_occurrence_single: tuple[str, str]) -> str:
    """TP id for the single-occurrence TP in train1."""
    return tp_occurrence_single[0]


@pytest.fixture
def tp_single_occurrence_id(tp_occurrence_single: tuple[str, str]) -> str:
    """Occurrence id for the single-occurrence TP in train1."""
    return tp_occurrence_single[1]


@pytest.fixture
def tp_occurrences_multi(synced_test_session: Session, example_multi_tp_orm: Example) -> list[tuple[str, str]]:
    """List of (tp_id, occurrence_id) for a multi-occurrence example in train1."""
    rows = (
        synced_test_session.query(TruePositiveOccurrenceORM.tp_id, TruePositiveOccurrenceORM.occurrence_id)
        .filter_by(snapshot_slug=example_multi_tp_orm.snapshot_slug)
        .order_by(TruePositiveOccurrenceORM.tp_id, TruePositiveOccurrenceORM.occurrence_id)
        .all()
    )
    assert rows, "Expected TP occurrences for multi-TP example in train1"
    return [(row.tp_id, row.occurrence_id) for row in rows]


@pytest.fixture
def fp_occurrence(synced_test_session: Session) -> tuple[str, str]:
    """FP occurrence (fp_id, occurrence_id) from git fixtures (fail fast if missing)."""
    row = (
        synced_test_session.query(FalsePositiveOccurrenceORM.fp_id, FalsePositiveOccurrenceORM.occurrence_id)
        .order_by(FalsePositiveOccurrenceORM.fp_id, FalsePositiveOccurrenceORM.occurrence_id)
        .first()
    )
    assert row is not None, "Expected at least one FP occurrence in git fixtures"
    return row.fp_id, row.occurrence_id


@pytest.fixture
def fp_id(fp_occurrence: tuple[str, str]) -> str:
    """FP id from git fixtures."""
    return fp_occurrence[0]


@pytest.fixture
def fp_occurrence_id(fp_occurrence: tuple[str, str]) -> str:
    """FP occurrence id from git fixtures."""
    return fp_occurrence[1]


def make_grader_run_with_credit(
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

    # Create reported issue
    issue = ReportedIssue(agent_run_id=critic_run.agent_run_id, issue_id=issue_id, rationale=f"Test issue {input_idx}")
    session.add(issue)
    occ = ReportedIssueOccurrence(
        agent_run_id=critic_run.agent_run_id,
        reported_issue_id=issue_id,
        locations=[DBLocationAnchor(file="subtract.py", start_line=1, end_line=1)],
    )
    session.add(occ)

    grader_run = make_grader_run(
        critic_run=critic_run, model=model, canonical_issues_snapshot=EMPTY_CANONICAL_ISSUES_SNAPSHOT
    )
    session.add(grader_run)
    session.flush()

    # Get snapshot_slug from critic's type_config
    snapshot_slug = critic_run.critic_config().example.snapshot_slug

    # Create grading edge directly
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


def _make_example_with_runs(slug: SnapshotSlug, credit: float) -> tuple[Example, AgentRun, AgentRun]:
    """Helper to create example with multiple critic and grader runs.

    Creates complete data including:
    - 2 Critic AgentRuns (for UCB/LCB computation which requires COUNT(*) > 1)
    - ReportedIssue + ReportedIssueOccurrence rows
    - 2 Grader AgentRuns
    - GradingEdge rows

    The grading_edges are populated using real TP IDs from the database.

    Args:
        slug: Snapshot slug to query
        credit: Credit value for grading edges (0.0-1.0)

    Returns:
        Tuple of (example, critic_run, grader_run) where runs are the first
        pair of AgentRun objects (second pair also created for UCB/LCB stats)
    """
    with get_session() as session:
        # Must use whole-snapshot example so recall_denominator matches all TPs
        example = session.query(Example).filter_by(snapshot_slug=slug, example_kind=ExampleKind.WHOLE_SNAPSHOT).first()
        assert example, f"No whole-snapshot example found for {slug}"

        # Get real TP occurrences from database - all TPs in snapshot
        tp_occs = get_tp_occurrences_for_snapshot(slug, session)
        assert tp_occs, f"No TP occurrences found for {slug}"
        assert len(tp_occs) == example.recall_denominator, (
            f"Mismatch: {len(tp_occs)} TP occurrences vs {example.recall_denominator} expected"
        )

        # Create first pair of runs
        critic_run, grader_run = make_critic_and_grader_run(
            example=example, tp_occurrences=tp_occs, credit=credit, session=session
        )

        # Create second pair of runs with slightly different credit
        # This is needed for UCB/LCB computation which requires COUNT(*) > 1
        make_critic_and_grader_run(example=example, tp_occurrences=tp_occs, credit=credit * 0.9, session=session)

        session.commit()

        return (example, critic_run, grader_run)


@pytest.fixture
def test_trivial_snapshot(synced_test_db: DatabaseConfig) -> Snapshot:
    """Provide the train1 snapshot (train split).

    This is a real git-tracked fixture with 2 TPs in add.py and subtract.py.
    Use this instead of creating synthetic snapshots.
    """
    with get_session() as session:
        snapshot = session.query(Snapshot).filter_by(slug="test-fixtures/train1").one()
        session.expunge(snapshot)
        return snapshot


@pytest.fixture
def test_validation_snapshot(synced_test_db: DatabaseConfig) -> Snapshot:
    """Provide the valid1 snapshot (valid split).

    This is a real git-tracked fixture with 1 TP in subtract.py.
    Use this instead of creating synthetic snapshots.
    """
    with get_session() as session:
        snapshot = session.query(Snapshot).filter_by(slug="test-fixtures/valid1").one()
        session.expunge(snapshot)
        return snapshot


@pytest.fixture
def test_train_example_with_runs(synced_test_db: DatabaseConfig) -> tuple[Example, AgentRun, AgentRun]:
    """Provide a train example with critic and grader runs.

    Uses train1 fixture (train split) and creates runs with 80% recall.
    Returns (example, critic_run, grader_run) tuple where runs are AgentRun objects.
    """
    return _make_example_with_runs(SnapshotSlug("test-fixtures/train1"), credit=0.8)


@pytest.fixture
def test_valid_example_with_runs(synced_test_db: DatabaseConfig) -> tuple[Example, AgentRun, AgentRun]:
    """Provide a valid example with critic and grader runs.

    Uses valid1 fixture (valid split) and creates runs with 60% recall.
    Returns (example, critic_run, grader_run) tuple where runs are AgentRun objects.
    """
    return _make_example_with_runs(SnapshotSlug("test-fixtures/valid1"), credit=0.6)


# =============================================================================
# Critic E2E Test Factories
# =============================================================================


@pytest.fixture
def run_critic_with_steps(synced_test_db, test_snapshot, make_step_runner, async_docker_client, test_workspace_manager):
    """Factory fixture for running critic with custom steps.

    Encapsulates common critic run setup so tests only need to provide steps.

    Note: This factory creates its own AgentRegistry per invocation rather than using
    the shared test_registry fixture. This is intentional because:
    - Factory returns a callable that may be invoked multiple times per test
    - Each invocation needs its own registry lifecycle with proper cleanup
    - The try/finally pattern ensures cleanup even if assertions fail mid-test

    For tests that need direct registry access (e.g., to call run_grader after
    run_critic_with_steps), use the shared test_registry fixture instead.

    Usage:
        async def test_critic_behavior(run_critic_with_steps):
            steps = [DockerExecCall(...)]
            # Must provide example parameter now
            with get_session() as session:
                example = session.query(Example).filter_by(
                    snapshot_slug="test-fixtures/train1",
                    scope_kind=ExampleKind.WHOLE_SNAPSHOT
                ).one()
                critic_run_id, status, runner = await run_critic_with_steps(steps, example=example.to_example_spec())
            assert status == AgentRunStatus.COMPLETED

    Returns:
        Factory function that accepts steps and example, returns
        (critic_run_id, status, runner) tuple.
    """

    async def _run(
        steps: list[Step],
        *,
        definition_id: str = CRITIC_AGENT_DEFINITION_ID,
        example: ExampleSpec | None = None,
        max_turns: int = 100,
    ) -> tuple[UUID, AgentRunStatus, object]:
        """Run critic with the given steps.

        Args:
            steps: Mock OpenAI response steps
            definition_id: Agent definition ID (default: "critic")
            example: ExampleSpec to review (default: whole-snapshot for test-trivial)
            max_turns: Maximum turns before timeout

        Returns:
            Tuple of (critic_run_id, status, runner)
        """
        # Default to whole-snapshot example if not provided
        if example is None:
            example = WholeSnapshotExample(snapshot_slug=test_snapshot)

        runner = make_step_runner(steps=steps)
        registry = AgentRegistry(
            docker_client=async_docker_client, db_config=synced_test_db, workspace_manager=test_workspace_manager
        )
        try:
            critic_run_id = await registry.run_critic(
                definition_id=definition_id, example=example, client=runner, max_turns=max_turns
            )
            # Query DB for status
            with get_session() as session:
                critic_run = session.get(AgentRun, critic_run_id)
                assert critic_run is not None
                status = critic_run.status
            return critic_run_id, status, runner
        finally:
            await registry.close()

    return _run


@pytest_asyncio.fixture
async def test_registry(synced_test_db, async_docker_client, test_workspace_manager):
    """Provide AgentRegistry for tests, handling cleanup."""
    registry = AgentRegistry(
        docker_client=async_docker_client, db_config=synced_test_db, workspace_manager=test_workspace_manager
    )
    yield registry
    await registry.close()


# =============================================================================
# Prompt Optimizer E2E Test Factories
# =============================================================================


@pytest.fixture
def run_prompt_optimizer_with_steps(synced_test_db, make_step_runner, make_openai_client, async_docker_client):
    """Factory fixture for running prompt optimizer with custom steps.

    Encapsulates common prompt optimizer run setup so tests only need to provide steps.

    Usage:
        async def test_optimizer_behavior(run_prompt_optimizer_with_steps, test_train_example_with_runs):
            steps = [DockerExecCall(...), AssertDockerExecThenCall(...)]
            await run_prompt_optimizer_with_steps(steps)
            # Assertions...

    Returns:
        Factory function that accepts steps and optional overrides.
    """

    async def _run(
        steps: list[Step],
        *,
        critic_steps: list[Step] | None = None,
        grader_steps: list[Step] | None = None,
        budget: float = 1.0,
        target_metric: TargetMetric = TargetMetric.WHOLE_REPO,
    ) -> None:
        """Run prompt optimizer with the given steps.

        Args:
            steps: Mock OpenAI response steps for the optimizer agent
            critic_steps: Optional steps for the critic agent (default: empty responses)
            grader_steps: Optional steps for the grader agent (default: empty responses)
            budget: Token budget (default: 1.0)
            target_metric: Target metric for optimization (default: WHOLE_REPO)
        """
        runner = make_step_runner(steps=steps)

        # Build critic client from steps or default to empty
        critic_client = make_step_runner(steps=critic_steps) if critic_steps else make_openai_client([])
        grader_client = make_step_runner(steps=grader_steps) if grader_steps else make_openai_client([])

        await run_prompt_optimizer(
            budget=budget,
            optimizer_client=runner,
            critic_client=critic_client,
            grader_client=grader_client,
            docker_client=async_docker_client,
            target_metric=target_metric,
            db_config=synced_test_db,
        )

    return _run


# =============================================================================
# Improvement Agent E2E Test Factories
# =============================================================================


@pytest.fixture
def success_termination() -> TerminationSuccess:
    return TerminationSuccess(definition_id="test-improved-critic", total_credit=2.0, baseline_avg=1.0)


@pytest.fixture
def run_improvement_agent_with_steps(
    synced_test_db,
    make_step_runner,
    async_docker_client,
    success_termination,
    subtract_file_example,
    noop_openai_client,
):
    """Factory fixture for running improvement agent with custom steps."""

    async def _run(
        steps: list[Step],
        *,
        token_budget: int = 100_000,
        model: str = "gpt-5-nano",
        baseline_definition_ids: list[str] | None = None,
    ):
        if baseline_definition_ids is None:
            baseline_definition_ids = [CRITIC_AGENT_DEFINITION_ID]

        runner = make_step_runner(steps=steps)

        def mock_check_termination(session, improvement_run_id, type_config):
            return success_termination

        with patch(
            "props.prompt_improve.reminder_handler.check_termination_condition", side_effect=mock_check_termination
        ):
            return await run_improvement_agent(
                examples=[subtract_file_example],
                baseline_definition_ids=baseline_definition_ids,
                token_budget=token_budget,
                model=model,
                docker_client=async_docker_client,
                db_config=synced_test_db,
                client=runner,
                critic_client=noop_openai_client,
                grader_client=noop_openai_client,
            )

    return _run
