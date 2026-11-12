"""SQLAlchemy evaluation models; see db/README.md and AGENTS.md for setup/RLS."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Enum,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from openai_utils.api_shape import LLMApiShape
from props.core.agent_types import AgentType, CriticTypeConfig, GraderTypeConfig, TypeConfig
from props.core.ids import SnapshotSlug, _SnapshotSlugBase
from props.core.models.examples import ExampleKind
from props.core.splits import Split
from props.db.snapshots import LocationAnchor
from util.sqlalchemy_types import StrEnumColumn, StringBackedStrEnumColumn

# Kept in sync with LLMApiShape so constraints accept each supported API shape.
_LLM_API_SHAPES_SQL = ", ".join(f"'{shape.value}'" for shape in LLMApiShape)

# Reusable SQLAlchemy Enum type for ExampleKind
# Use this instead of mapped_column(String) for proper Python enum conversion
EXAMPLE_KIND_ENUM_TYPE = Enum(
    ExampleKind,
    name="example_kind_enum",
    create_constraint=False,  # VIEW models, enum type already exists
    values_callable=lambda x: [e.value for e in x],  # Use enum value not name
)


class StatsWithCI(BaseModel):
    """Statistics with 95% confidence interval bounds. Maps to PostgreSQL composite type stats_with_ci.

    lcb95/ucb95 are mean +/- 1.96 * stddev/sqrt(n); NULL if n < 2.
    """

    n: int
    mean: float
    min: float
    max: float
    lcb95: float | None
    ucb95: float | None


class StatsWithCIType(TypeDecorator[StatsWithCI | None]):
    """Adapt psycopg2 register_composite() named tuples to StatsWithCI.

    Text is a placeholder; database.py registers the actual stats_with_ci composite.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(
        self, value: StatsWithCI | None, dialect: Any
    ) -> tuple[int, float, float, float, float | None, float | None] | None:
        if value is None:
            return None
        return (value.n, value.mean, value.min, value.max, value.lcb95, value.ucb95)

    def process_result_value(self, value: Any, dialect: Any) -> StatsWithCI | None:
        if value is None:
            return None
        return StatsWithCI(
            n=value.n, mean=value.mean, min=value.min, max=value.max, lcb95=value.lcb95, ucb95=value.ucb95
        )


class AgentRunStatus(StrEnum):
    """Terminal states are EXITED (inspect container_exit_code), TIMED_OUT, CANCELLED and STARTUP_FAILED."""

    IN_PROGRESS = "in_progress"
    EXITED = "exited"
    TIMED_OUT = "timed_out"
    # The run's host task was cancelled before the container finished — e.g. the
    # GraderSupervisor replacing a snapshot grader during reconcile, or shutdown.
    # The pod is deleted; this records a terminal status so the run doesn't leak
    # as IN_PROGRESS forever.
    CANCELLED = "cancelled"
    STARTUP_FAILED = "startup_failed"


class PydanticColumn[T: BaseModel](TypeDecorator[T]):
    """JSONB adapter for Pydantic models or unions; pass the type directly.

    TODO: Type the remaining structured dict JSONB columns with PydanticColumn.
    """

    impl = JSONB
    cache_ok = True

    def __init__(self, pydantic_type: type[T] | Any):
        super().__init__()
        self._adapter: TypeAdapter[T] = TypeAdapter(pydantic_type)

    def process_bind_param(self, value: T | None, dialect: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        # Use TypeAdapter.dump_python for all types (handles BaseModel and unions)
        # warnings=False suppresses harmless union variant checking warnings
        return self._adapter.dump_python(value, mode="json", by_alias=True, warnings=False)  # type: ignore[no-any-return]

    def process_result_value(self, value: dict[str, Any] | None, dialect: Any) -> T | None:
        if value is None:
            return None
        return self._adapter.validate_python(value)


class SnapshotSlugColumn(TypeDecorator[SnapshotSlug]):
    """Validate stored strings as SnapshotSlug on load."""

    impl = String
    cache_ok = True

    def __init__(self):
        super().__init__()
        self._adapter: TypeAdapter[_SnapshotSlugBase] = TypeAdapter(_SnapshotSlugBase)

    def process_bind_param(self, value: SnapshotSlug | str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        # SnapshotSlug is a NewType over validated string, so it's already a string at runtime
        return str(value)

    def process_result_value(self, value: str | None, dialect: Any) -> SnapshotSlug | None:
        if value is None:
            return None
        validated = self._adapter.validate_python(value)
        return SnapshotSlug(validated)


class PathColumn(TypeDecorator[Path]):
    impl = String
    cache_ok = True

    def process_bind_param(self, value: Path | str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    def process_result_value(self, value: str | None, dialect: Any) -> Path | None:
        if value is None:
            return None
        return Path(value)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[type, Any]] = {
        dict[str, Any]: JSONB,
        UUID: PG_UUID(as_uuid=True),
        SnapshotSlug: SnapshotSlugColumn(),
        Split: StrEnumColumn(Split, name="split_enum"),
        AgentRunStatus: StrEnumColumn(AgentRunStatus, name="agent_run_status_enum"),
    }


class Snapshot(Base):
    """Source of truth for snapshot-to-split assignment."""

    __tablename__ = "snapshots"

    slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), primary_key=True)
    split: Mapped[Split] = mapped_column(nullable=False)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, comment="tar archive of source code")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    true_positives: Mapped[list[TruePositive]] = relationship(
        back_populates="snapshot_obj", cascade="all, delete-orphan"
    )
    false_positives: Mapped[list[FalsePositive]] = relationship(
        back_populates="snapshot_obj", cascade="all, delete-orphan"
    )

    @classmethod
    def get(cls, slug: SnapshotSlug) -> Snapshot | None:
        session = Session.object_session(cls)
        if session is None:
            raise RuntimeError("Model not bound to session")
        return session.execute(select(cls).where(cls.slug == slug)).scalar_one_or_none()


class TruePositive(Base):
    """Expected finding; critic_scopes_expected_to_recall supplies its recall scopes."""

    __tablename__ = "true_positives"
    __table_args__ = (
        CheckConstraint("tp_id ~ '^[a-z0-9_-]+$' AND length(tp_id) >= 5 AND length(tp_id) <= 40", name="tp_id_format"),
    )

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(
        SnapshotSlugColumn(), ForeignKey("snapshots.slug", ondelete="RESTRICT"), primary_key=True
    )
    tp_id: Mapped[str] = mapped_column(String, primary_key=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    snapshot_obj: Mapped[Snapshot] = relationship(back_populates="true_positives")
    occurrences: Mapped[list[TruePositiveOccurrenceORM]] = relationship(
        back_populates="true_positive", cascade="all, delete-orphan"
    )

    @classmethod
    def get(cls, snapshot_slug: SnapshotSlug, tp_id: str) -> TruePositive | None:
        session = Session.object_session(cls)
        if session is None:
            raise RuntimeError("Model not bound to session")
        return session.execute(
            select(cls).where(cls.snapshot_slug == snapshot_slug, cls.tp_id == tp_id)
        ).scalar_one_or_none()


class FalsePositive(Base):
    """Known non-issue; occurrences carry relevant-file semantics."""

    __tablename__ = "false_positives"
    __table_args__ = (
        CheckConstraint("fp_id ~ '^[a-z0-9_-]+$' AND length(fp_id) >= 5 AND length(fp_id) <= 40", name="fp_id_format"),
        {"comment": "Patterns the labeler considers acceptable - teaches agents what NOT to flag."},
    )

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(
        SnapshotSlugColumn(), ForeignKey("snapshots.slug", ondelete="RESTRICT"), primary_key=True
    )
    fp_id: Mapped[str] = mapped_column(String, primary_key=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    snapshot_obj: Mapped[Snapshot] = relationship(back_populates="false_positives")
    occurrences: Mapped[list[FalsePositiveOccurrenceORM]] = relationship(
        back_populates="false_positive", cascade="all, delete-orphan"
    )

    @classmethod
    def get(cls, snapshot_slug: SnapshotSlug, fp_id: str) -> FalsePositive | None:
        session = Session.object_session(cls)
        if session is None:
            raise RuntimeError("Model not bound to session")
        return session.execute(
            select(cls).where(cls.snapshot_slug == snapshot_slug, cls.fp_id == fp_id)
        ).scalar_one_or_none()


class TruePositiveOccurrenceORM(Base):
    """Expected occurrence with normalized ranges and alternative recall scopes."""

    __tablename__ = "true_positive_occurrences"

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), primary_key=True)
    tp_id: Mapped[str] = mapped_column(String, primary_key=True)
    occurrence_id: Mapped[str] = mapped_column(String, primary_key=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    match_file_restriction: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_slug", "tp_id"], ["true_positives.snapshot_slug", "true_positives.tp_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["snapshot_slug", "match_file_restriction"],
            ["file_sets.snapshot_slug", "file_sets.files_hash"],
            ondelete="SET NULL",
        ),
    )

    true_positive: Mapped[TruePositive] = relationship(back_populates="occurrences")
    critic_scopes_expected_to_recall: Mapped[list[CriticScopeExpectedToRecall]] = relationship(
        back_populates="occurrence",
        cascade="all, delete-orphan",
        primaryjoin="and_(TruePositiveOccurrenceORM.snapshot_slug == foreign(CriticScopeExpectedToRecall.snapshot_slug), "
        "TruePositiveOccurrenceORM.tp_id == foreign(CriticScopeExpectedToRecall.tp_id), "
        "TruePositiveOccurrenceORM.occurrence_id == foreign(CriticScopeExpectedToRecall.occurrence_id))",
    )
    # overlaps: occurrence_ranges table has exclusive arc (tp_id XOR fp_id), so both
    # TruePositiveOccurrenceORM.ranges and FalsePositiveOccurrenceORM.ranges write to same columns
    ranges: Mapped[list[OccurrenceRangeORM]] = relationship(
        back_populates="tp_occurrence",
        cascade="all, delete-orphan",
        foreign_keys="[OccurrenceRangeORM.snapshot_slug, OccurrenceRangeORM.tp_id, OccurrenceRangeORM.occurrence_id]",
        overlaps="ranges,fp_occurrence",
    )

    @property
    def critic_scopes_expected_to_recall_set(self) -> set[frozenset[Path]]:
        result: set[frozenset[Path]] = set()
        for scope in self.critic_scopes_expected_to_recall:
            # Each scope links to a file_set via files_hash
            # Get file paths from file_set_members
            if scope.file_set:
                file_paths = frozenset(Path(m.file_path) for m in scope.file_set.members)
                result.add(file_paths)
        return result


class FalsePositiveOccurrenceORM(Base):
    """Known non-issue occurrence with normalized ranges and relevant files."""

    __tablename__ = "false_positive_occurrences"

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), primary_key=True)
    fp_id: Mapped[str] = mapped_column(String, primary_key=True)
    occurrence_id: Mapped[str] = mapped_column(String, primary_key=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    match_file_restriction: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_slug", "fp_id"], ["false_positives.snapshot_slug", "false_positives.fp_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["snapshot_slug", "match_file_restriction"],
            ["file_sets.snapshot_slug", "file_sets.files_hash"],
            ondelete="SET NULL",
        ),
    )

    false_positive: Mapped[FalsePositive] = relationship(back_populates="occurrences")
    # overlaps: occurrence_ranges table has exclusive arc (tp_id XOR fp_id), so both
    # TruePositiveOccurrenceORM.ranges and FalsePositiveOccurrenceORM.ranges write to same columns
    ranges: Mapped[list[OccurrenceRangeORM]] = relationship(
        back_populates="fp_occurrence",
        cascade="all, delete-orphan",
        foreign_keys="[OccurrenceRangeORM.snapshot_slug, OccurrenceRangeORM.fp_id, OccurrenceRangeORM.occurrence_id]",
        overlaps="ranges,tp_occurrence",
    )
    relevant_file_orms: Mapped[list[FalsePositiveRelevantFileORM]] = relationship(
        back_populates="occurrence", cascade="all, delete-orphan"
    )


class OccurrenceRangeORM(Base):
    """Line range within a TP or FP occurrence (exclusive arc pattern)."""

    __tablename__ = "occurrence_ranges"

    # Auto-increment PK; uniqueness enforced by uq_occurrence_ranges constraint
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), nullable=False)
    # Exactly one of tp_id/fp_id is set (exclusive arc); the other is NULL
    tp_id: Mapped[str | None] = mapped_column(String, nullable=True)
    fp_id: Mapped[str | None] = mapped_column(String, nullable=True)
    occurrence_id: Mapped[str] = mapped_column(String, nullable=False)
    file_path: Mapped[Path] = mapped_column(PathColumn(), nullable=False)
    range_id: Mapped[int] = mapped_column(Integer, nullable=False)
    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_slug", "tp_id", "occurrence_id"],
            [
                "true_positive_occurrences.snapshot_slug",
                "true_positive_occurrences.tp_id",
                "true_positive_occurrences.occurrence_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["snapshot_slug", "fp_id", "occurrence_id"],
            [
                "false_positive_occurrences.snapshot_slug",
                "false_positive_occurrences.fp_id",
                "false_positive_occurrences.occurrence_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["snapshot_slug", "file_path"],
            ["snapshot_files.snapshot_slug", "snapshot_files.file_path"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "snapshot_slug", "tp_id", "fp_id", "occurrence_id", "file_path", "range_id", name="uq_occurrence_ranges"
        ),
        CheckConstraint("(tp_id IS NULL) <> (fp_id IS NULL)", name="occurrence_range_exclusive_arc"),
        CheckConstraint("start_line IS NULL OR start_line >= 1", name="occurrence_range_start_line_positive"),
        CheckConstraint(
            "(start_line IS NULL AND end_line IS NULL) "
            "OR (start_line IS NOT NULL AND (end_line IS NULL OR end_line >= start_line))",
            name="occurrence_range_end_gte_start",
        ),
    )

    # Relationships - use foreign() to specify which columns to join on
    # overlaps silences SQLAlchemy warning about shared columns (snapshot_slug, occurrence_id)
    # This is an exclusive arc pattern: exactly one of tp_occurrence/fp_occurrence is set
    tp_occurrence: Mapped[TruePositiveOccurrenceORM | None] = relationship(
        back_populates="ranges", foreign_keys=[snapshot_slug, tp_id, occurrence_id], overlaps="fp_occurrence,ranges"
    )
    fp_occurrence: Mapped[FalsePositiveOccurrenceORM | None] = relationship(
        back_populates="ranges", foreign_keys=[snapshot_slug, fp_id, occurrence_id], overlaps="tp_occurrence,ranges"
    )


class FalsePositiveRelevantFileORM(Base):
    __tablename__ = "fp_occurrence_relevant_files"

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), primary_key=True)
    fp_id: Mapped[str] = mapped_column(String, primary_key=True)
    occurrence_id: Mapped[str] = mapped_column(String, primary_key=True)
    file_path: Mapped[Path] = mapped_column(PathColumn(), primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_slug", "fp_id", "occurrence_id"],
            [
                "false_positive_occurrences.snapshot_slug",
                "false_positive_occurrences.fp_id",
                "false_positive_occurrences.occurrence_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["snapshot_slug", "file_path"],
            ["snapshot_files.snapshot_slug", "snapshot_files.file_path"],
            ondelete="CASCADE",
        ),
    )

    occurrence: Mapped[FalsePositiveOccurrenceORM] = relationship(back_populates="relevant_file_orms")


class SnapshotFile(Base):
    """Populated from the snapshot code tar during sync; validates occurrence/scope paths via FKs."""

    __tablename__ = "snapshot_files"

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(
        SnapshotSlugColumn(), ForeignKey("snapshots.slug", ondelete="CASCADE"), primary_key=True
    )
    file_path: Mapped[str] = mapped_column(
        String, primary_key=True, comment='Path relative to snapshot root (e.g., "src/utils.py"). NOT absolute paths.'
    )
    line_count: Mapped[int] = mapped_column(Integer, nullable=False)

    snapshot_obj: Mapped[Snapshot] = relationship()


class FileSet(Base):
    """Content-addressed scope: files_hash is MD5 of sorted paths, deduplicated per snapshot."""

    __tablename__ = "file_sets"

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(
        SnapshotSlugColumn(), ForeignKey("snapshots.slug", ondelete="CASCADE"), primary_key=True
    )
    files_hash: Mapped[str] = mapped_column(String, primary_key=True, comment="MD5 of sorted file paths")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())

    snapshot_obj: Mapped[Snapshot] = relationship()
    members: Mapped[list[FileSetMember]] = relationship(back_populates="file_set", cascade="all")


class FileSetMember(Base):
    __tablename__ = "file_set_members"

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), primary_key=True)
    files_hash: Mapped[str] = mapped_column(String, primary_key=True)
    file_path: Mapped[str] = mapped_column(String, primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_slug", "files_hash"], ["file_sets.snapshot_slug", "file_sets.files_hash"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["snapshot_slug", "file_path"],
            ["snapshot_files.snapshot_slug", "snapshot_files.file_path"],
            ondelete="CASCADE",
        ),
    )

    file_set: Mapped[FileSet] = relationship(back_populates="members")


class CriticScopeExpectedToRecall(Base):
    """Alternative expected scopes (OR) determine only the recall denominator.

    Critics can find issues outside these scopes (recall >100% is possible). Unlike
    match_file_restriction, these scopes do not constrain where graders give credit.
    """

    __tablename__ = "critic_scopes_expected_to_recall"

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), primary_key=True)
    tp_id: Mapped[str] = mapped_column(String, primary_key=True)
    occurrence_id: Mapped[str] = mapped_column(String, primary_key=True)
    files_hash: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_slug", "tp_id", "occurrence_id"],
            [
                "true_positive_occurrences.snapshot_slug",
                "true_positive_occurrences.tp_id",
                "true_positive_occurrences.occurrence_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["snapshot_slug", "files_hash"], ["file_sets.snapshot_slug", "file_sets.files_hash"], ondelete="CASCADE"
        ),
    )

    # overlaps needed because snapshot_slug is in both FKs (to occurrence and file_set)
    file_set: Mapped[FileSet] = relationship(overlaps="critic_scopes_expected_to_recall,occurrence")
    occurrence: Mapped[TruePositiveOccurrenceORM] = relationship(
        back_populates="critic_scopes_expected_to_recall", overlaps="file_set"
    )


class ReportedIssue(Base):
    """Agent-reported issue; agents hard-delete incorrect reports."""

    __tablename__ = "reported_issues"
    __table_args__ = (
        CheckConstraint(
            "issue_id ~ '^[a-z0-9_-]+$' AND length(issue_id) >= 5 AND length(issue_id) <= 40", name="issue_id_format"
        ),
    )

    agent_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agent_runs.agent_run_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
        server_default=FetchedValue(),
    )
    issue_id: Mapped[str] = mapped_column(String, primary_key=True, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())

    agent_run: Mapped[AgentRun] = relationship(back_populates="reported_issues")
    occurrences: Mapped[list[ReportedIssueOccurrence]] = relationship(
        back_populates="reported_issue", cascade="all, delete-orphan"
    )


class ReportedIssueOccurrence(Base):
    """One or more JSONB locations: {file: str, start_line?: int, end_line?: int}."""

    __tablename__ = "reported_issue_occurrences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, server_default=FetchedValue())
    reported_issue_id: Mapped[str] = mapped_column(String, nullable=False)

    locations: Mapped[list[LocationAnchor]] = mapped_column(
        PydanticColumn(list[LocationAnchor]),
        nullable=False,
        comment="1+ location anchors: {file, start_line?, end_line?}",
    )

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())
    cancelled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_run_id", "reported_issue_id"],
            ["reported_issues.agent_run_id", "reported_issues.issue_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("jsonb_array_length(locations) > 0", name="locations_not_empty"),
    )

    reported_issue: Mapped[ReportedIssue] = relationship(back_populates="occurrences")


class GradingPending(Base):
    """Missing (critique issue, GT occurrence) edges; empty within grader scope means complete."""

    __tablename__ = "grading_pending"
    __table_args__ = ({"info": {"is_view": True}},)

    # Composite primary key for view (SQLAlchemy requires PK)
    critique_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    critique_issue_id: Mapped[str] = mapped_column(String, primary_key=True)
    tp_id: Mapped[str | None] = mapped_column(String, primary_key=True, nullable=True)
    tp_occurrence_id: Mapped[str | None] = mapped_column(String, primary_key=True, nullable=True)
    fp_id: Mapped[str | None] = mapped_column(String, primary_key=True, nullable=True)
    fp_occurrence_id: Mapped[str | None] = mapped_column(String, primary_key=True, nullable=True)

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn())


class TpTarget(BaseModel):
    kind: Literal["tp"] = "tp"
    tp_id: str
    occurrence_id: str
    credit: float


class FpTarget(BaseModel):
    kind: Literal["fp"] = "fp"
    fp_id: str
    occurrence_id: str
    credit: float


GradingTarget = Annotated[TpTarget | FpTarget, Field(discriminator="kind")]


class GradingEdge(Base):
    """Complete bipartite critique-issue/GT-occurrence coverage; grading_pending finds missing edges.

    TP credit measures match quality (0 means reviewed/no match); positive FP credit
    is a penalty. Exactly one target is allowed; most edges have zero credit.
    """

    __tablename__ = "grading_edges"
    __table_args__ = (
        CheckConstraint(
            "(tp_id IS NOT NULL AND tp_occurrence_id IS NOT NULL AND fp_id IS NULL AND fp_occurrence_id IS NULL) "
            "OR (fp_id IS NOT NULL AND fp_occurrence_id IS NOT NULL AND tp_id IS NULL AND tp_occurrence_id IS NULL)",
            name="exactly_one_target_edge",
        ),
        CheckConstraint("credit >= 0.0 AND credit <= 1.0", name="credit_range_edge"),
        {"comment": "Bipartite graph edges from critique issues to GT occurrences."},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    critique_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    critique_issue_id: Mapped[str] = mapped_column(String, nullable=False)

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), nullable=False)

    tp_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tp_occurrence_id: Mapped[str | None] = mapped_column(String, nullable=True)

    fp_id: Mapped[str | None] = mapped_column(String, nullable=True)
    fp_occurrence_id: Mapped[str | None] = mapped_column(String, nullable=True)

    credit: Mapped[float] = mapped_column(nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    grader_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agent_runs.agent_run_id", ondelete="CASCADE"), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())

    grader_run: Mapped[AgentRun] = relationship(foreign_keys=[grader_run_id])

    def to_target(self) -> GradingTarget:
        """Convert to grading target. Exactly one of TP or FP must be set (DB constraint)."""
        if self.tp_id is not None:
            assert self.tp_occurrence_id is not None, (
                f"TP grading edge {self.critique_issue_id} missing tp_occurrence_id"
            )
            return TpTarget(tp_id=self.tp_id, occurrence_id=self.tp_occurrence_id, credit=self.credit)
        if self.fp_id is not None:
            assert self.fp_occurrence_id is not None, (
                f"FP grading edge {self.critique_issue_id} missing fp_occurrence_id"
            )
            return FpTarget(fp_id=self.fp_id, occurrence_id=self.fp_occurrence_id, credit=self.credit)
        raise ValueError(f"Grading edge {self.critique_issue_id} has no target (DB constraint violation)")


class IssueCluster(Base):
    """Same novel finding across critic runs; only issues with all-zero grading edges qualify."""

    __tablename__ = "issue_clusters"

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(
        SnapshotSlugColumn(), ForeignKey("snapshots.slug", ondelete="CASCADE"), primary_key=True
    )
    cluster_id: Mapped[str] = mapped_column(String, primary_key=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    grader_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agent_runs.agent_run_id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())

    members: Mapped[list[IssueClusterMember]] = relationship(back_populates="cluster", cascade="all, delete-orphan")


class IssueClusterMember(Base):
    """An issue can belong to at most one cluster and must have no positive grading edges."""

    __tablename__ = "issue_cluster_members"

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), primary_key=True)
    cluster_id: Mapped[str] = mapped_column(String, primary_key=True)
    critique_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    critique_issue_id: Mapped[str] = mapped_column(String, primary_key=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    grader_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agent_runs.agent_run_id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_slug", "cluster_id"],
            ["issue_clusters.snapshot_slug", "issue_clusters.cluster_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["critique_run_id", "critique_issue_id"],
            ["reported_issues.agent_run_id", "reported_issues.issue_id"],
            ondelete="CASCADE",
        ),
        # Each critique issue belongs to at most one cluster
        UniqueConstraint("critique_run_id", "critique_issue_id", name="uq_issue_cluster_member_issue"),
    )

    cluster: Mapped[IssueCluster] = relationship(back_populates="members")


class ClusteringPending(Base):
    """Fully graded, unmatched, unclustered issues; empty per snapshot means clustering is complete."""

    __tablename__ = "clustering_pending"
    __table_args__ = ({"info": {"is_view": True}},)

    critique_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    critique_issue_id: Mapped[str] = mapped_column(String, primary_key=True)
    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn())


class ModelMetadata(Base):
    """Synced from PropsConfig.models, with explicit upstream configuration.

    upstream_name selects [upstreams.*]; null upstream_model sends model_id.
    """

    __tablename__ = "model_metadata"

    model_id: Mapped[str] = mapped_column(String, primary_key=True)
    input_usd_per_1m_tokens: Mapped[float] = mapped_column(nullable=False)
    cached_input_usd_per_1m_tokens: Mapped[float | None] = mapped_column(nullable=True)
    output_usd_per_1m_tokens: Mapped[float] = mapped_column(nullable=False)
    context_window_tokens: Mapped[int] = mapped_column(nullable=False)
    max_output_tokens: Mapped[int | None] = mapped_column(nullable=True)
    upstream_name: Mapped[str] = mapped_column(String, nullable=False)
    upstream_model: Mapped[str | None] = mapped_column(String, nullable=True)
    api_shape: Mapped[LLMApiShape] = mapped_column(
        StringBackedStrEnumColumn(LLMApiShape), nullable=False, server_default=LLMApiShape.RESPONSES.value
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (CheckConstraint(f"api_shape IN ({_LLM_API_SHAPES_SQL})", name="model_metadata_api_shape_check"),)


class LLMRunCost(Base):
    """Proxy-logged token usage and costs aggregated by (agent_run_id, model)."""

    __tablename__ = "llm_run_costs"
    __table_args__ = {"info": {"is_view": True}, "extend_existing": True}  # noqa: RUF012

    # Tell SQLAlchemy NOT to create this as a table
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    agent_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    model: Mapped[str] = mapped_column(String, primary_key=True)
    input_tokens: Mapped[int] = mapped_column(nullable=True)
    cached_input_tokens: Mapped[int] = mapped_column(nullable=True)
    output_tokens: Mapped[int] = mapped_column(nullable=True)
    cost_usd: Mapped[float] = mapped_column(nullable=True)
    request_count: Mapped[int] = mapped_column(nullable=True)


class AgentRunBudgetStatus(Base):
    """own_spent_usd counts direct costs; tree_spent_usd includes all descendants.

    remaining_usd = budget_usd - tree_spent_usd.
    """

    __tablename__ = "agent_run_budget_status"
    __table_args__ = {"info": {"is_view": True}, "extend_existing": True}  # noqa: RUF012
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    agent_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    budget_usd: Mapped[float] = mapped_column(nullable=False)
    own_spent_usd: Mapped[float] = mapped_column(nullable=False)
    tree_spent_usd: Mapped[float] = mapped_column(nullable=False)
    remaining_usd: Mapped[float] = mapped_column(nullable=False)


class TpOccurrenceCredit(Base):
    """SUM of edge credits per (critic_run, tp, occurrence), capped at 1 by a DB trigger.

    grader_run_id is provenance, not an aggregation dimension.
    """

    __tablename__ = "tp_occurrence_credits"
    __table_args__ = {"info": {"is_view": True}, "extend_existing": True}  # noqa: RUF012
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    critic_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tp_id: Mapped[str] = mapped_column(String, primary_key=True)
    occurrence_id: Mapped[str] = mapped_column(String, primary_key=True)

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), nullable=False)
    split: Mapped[Split] = mapped_column(nullable=False)
    example_kind: Mapped[ExampleKind] = mapped_column(EXAMPLE_KIND_ENUM_TYPE, nullable=False)
    files_hash: Mapped[str | None] = mapped_column(String, nullable=True)  # NULL for whole_snapshot

    critic_image_digest: Mapped[str] = mapped_column(String, nullable=False)
    critic_model: Mapped[str] = mapped_column(String, nullable=False)

    # Credit (sum of all grading edges for this critique+occurrence, ≤ 1.0)
    found_credit: Mapped[float] = mapped_column(nullable=False)


class RecallByRun(Base):
    """Includes in-progress and terminal runs; zero missing_grading_edges means grading is complete."""

    __tablename__ = "recall_by_run"
    __table_args__ = {"info": {"is_view": True}, "extend_existing": True}  # noqa: RUF012
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    critic_run_id: Mapped[UUID] = mapped_column(primary_key=True)

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), nullable=False)
    example_kind: Mapped[ExampleKind] = mapped_column(EXAMPLE_KIND_ENUM_TYPE, nullable=False)
    files_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    split: Mapped[Split] = mapped_column(nullable=False)
    recall_denominator: Mapped[int] = mapped_column(Integer, nullable=False)

    critic_image_digest: Mapped[str] = mapped_column(String, nullable=False)
    critic_model: Mapped[str] = mapped_column(String, nullable=False)
    critic_status: Mapped[AgentRunStatus] = mapped_column(nullable=False)

    # Scalar credit (sum of all TP occurrence credits for this critic run)
    total_credit: Mapped[float] = mapped_column(nullable=False)

    # Scalar recall (total_credit / recall_denominator)
    recall: Mapped[float] = mapped_column(nullable=False)

    # Number of grading_pending edges remaining for this run (0 = complete)
    missing_grading_edges: Mapped[int] = mapped_column(Integer, nullable=False)


class RecallByDefinitionExample(Base):
    """Aggregate recall_by_run by (definition, model, example).

    credit_stats summarizes raw credits; recall_stats divides by recall_denominator.
    """

    __tablename__ = "recall_by_definition_example"
    __table_args__ = {"info": {"is_view": True}, "extend_existing": True}  # noqa: RUF012
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    critic_image_digest: Mapped[str] = mapped_column(String, primary_key=True)
    critic_model: Mapped[str] = mapped_column(String, primary_key=True)
    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), primary_key=True)
    example_kind: Mapped[ExampleKind] = mapped_column(EXAMPLE_KIND_ENUM_TYPE, primary_key=True)
    files_hash: Mapped[str | None] = mapped_column(String, primary_key=True)
    split: Mapped[Split] = mapped_column(nullable=False)

    recall_denominator: Mapped[int] = mapped_column(Integer, nullable=False)

    n_runs: Mapped[int] = mapped_column(Integer, nullable=False)

    status_counts: Mapped[dict[AgentRunStatus, int]] = mapped_column(JSONB, nullable=False)

    # Credit stats (numerator; failed runs count as 0 credit)
    credit_stats: Mapped[StatsWithCI | None] = mapped_column(StatsWithCIType(), nullable=True)

    recall_stats: Mapped[StatsWithCI | None] = mapped_column(StatsWithCIType(), nullable=True)


class RecallByDefinitionSplitKind(Base):
    """Aggregate examples within (definition, model, split, example_kind).

    recall_denominator sums distinct examples; recall_stats = credit_stats / denominator.
    """

    __tablename__ = "recall_by_definition_split_kind"
    __table_args__ = {"info": {"is_view": True}, "extend_existing": True}  # noqa: RUF012
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    split: Mapped[Split] = mapped_column(primary_key=True)
    example_kind: Mapped[ExampleKind] = mapped_column(EXAMPLE_KIND_ENUM_TYPE, primary_key=True)
    critic_image_digest: Mapped[str] = mapped_column(String, primary_key=True)
    critic_model: Mapped[str] = mapped_column(String, primary_key=True)

    n_examples: Mapped[int] = mapped_column(Integer, nullable=False)
    n_runs: Mapped[int] = mapped_column(Integer, nullable=False)

    # Catchable occurrences (denominator - sum across distinct examples)
    recall_denominator: Mapped[int] = mapped_column(Integer, nullable=False)

    status_counts: Mapped[dict[AgentRunStatus, int]] = mapped_column(JSONB, nullable=False)

    # Credit stats (numerator; failed runs count as 0 credit)
    credit_stats: Mapped[StatsWithCI | None] = mapped_column(StatsWithCIType(), nullable=True)

    recall_stats: Mapped[StatsWithCI | None] = mapped_column(StatsWithCIType(), nullable=True)

    # Count of runs where caught credit was exactly 0 (complete failure to find anything)
    zero_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class RecallByExample(Base):
    """Aggregate definition/example statistics across definitions.

    recall_denominator counts GT; recall_stats = credit_stats / recall_denominator.
    """

    __tablename__ = "recall_by_example"
    __table_args__ = {"info": {"is_view": True}, "extend_existing": True}  # noqa: RUF012
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), primary_key=True)
    example_kind: Mapped[ExampleKind] = mapped_column(EXAMPLE_KIND_ENUM_TYPE, primary_key=True)
    files_hash: Mapped[str | None] = mapped_column(String, primary_key=True)
    split: Mapped[Split] = mapped_column(primary_key=True)
    critic_model: Mapped[str] = mapped_column(String, primary_key=True)

    recall_denominator: Mapped[int] = mapped_column(Integer, nullable=False)

    n_runs: Mapped[int] = mapped_column(Integer, nullable=False)

    status_counts: Mapped[dict[AgentRunStatus, int]] = mapped_column(JSONB, nullable=False)

    # Credit stats (numerator; failed runs count as 0 credit)
    credit_stats: Mapped[StatsWithCI | None] = mapped_column(StatsWithCIType(), nullable=True)

    recall_stats: Mapped[StatsWithCI | None] = mapped_column(StatsWithCIType(), nullable=True)


class WinningDefinition(BaseModel):
    image_digest: str
    credit_stats: StatsWithCI
    n_runs: int


_WinningDefinitionListAdapter: TypeAdapter[list[WinningDefinition]] = TypeAdapter(list[WinningDefinition])


class ParetoFrontierByExample(Base):
    """Definitions tied at best mean credit for each (snapshot, split, kind, files_hash, model).

    Built on recall_by_definition_example; failed/timed-out runs count as zero credit.
    winning_definitions share the best credit_stats.mean; divide by recall_denominator
    for recall. Each winner carries image_digest, credit_stats and n_runs.
    """

    __tablename__ = "pareto_frontier_by_example"
    __table_args__ = {"info": {"is_view": True}, "extend_existing": True}  # noqa: RUF012
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), primary_key=True)
    split: Mapped[Split] = mapped_column(primary_key=True)
    example_kind: Mapped[ExampleKind] = mapped_column(EXAMPLE_KIND_ENUM_TYPE, primary_key=True)
    files_hash: Mapped[str | None] = mapped_column(String, primary_key=True)
    critic_model: Mapped[str] = mapped_column(String, primary_key=True)

    recall_denominator: Mapped[int] = mapped_column(Integer, nullable=False)

    _winning_definitions_raw: Mapped[list[dict[str, Any]]] = mapped_column("winning_definitions", JSONB, nullable=False)

    @property
    def winning_definitions(self) -> list[WinningDefinition]:
        return _WinningDefinitionListAdapter.validate_python(self._winning_definitions_raw)

    @property
    def best_mean_credit(self) -> float:
        return self.winning_definitions[0].credit_stats.mean


class OccurrenceStatistics(Base):
    """Per-occurrence/run statistics: credit_stats.n is critic run count, .mean is average credit."""

    __tablename__ = "occurrence_statistics"
    __table_args__ = {"info": {"is_view": True}, "extend_existing": True}  # noqa: RUF012
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), primary_key=True)
    split: Mapped[Split] = mapped_column(primary_key=True)
    example_kind: Mapped[ExampleKind] = mapped_column(EXAMPLE_KIND_ENUM_TYPE, primary_key=True)
    files_hash: Mapped[str | None] = mapped_column(String, primary_key=True)

    tp_id: Mapped[str] = mapped_column(String, primary_key=True)
    occurrence_id: Mapped[str] = mapped_column(String, primary_key=True)

    critic_image_digest: Mapped[str] = mapped_column(String, primary_key=True)
    critic_model: Mapped[str] = mapped_column(String, primary_key=True)

    # Credit statistics across critic runs (stats_with_ci: .n = critic run count, .mean = avg credit)
    credit_stats: Mapped[StatsWithCI | None] = mapped_column(StatsWithCIType(), nullable=True)


class LLMRequestCost(Base):
    __tablename__ = "llm_request_costs"
    __table_args__ = (
        {
            "info": {"is_view": True},
            "extend_existing": True,
            "comment": "Per-request LLM cost (joins llm_requests with model_metadata pricing).",
        },
    )
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    agent_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    input_tokens: Mapped[int] = mapped_column(nullable=True)
    cached_input_tokens: Mapped[int] = mapped_column(nullable=True)
    output_tokens: Mapped[int] = mapped_column(nullable=True)
    latency_ms: Mapped[int] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    cost_usd: Mapped[float] = mapped_column(nullable=True)


class ValidationRecallByDefinition(Base):
    __tablename__ = "validation_recall_by_definition"
    __table_args__ = (
        {
            "info": {"is_view": True},
            "extend_existing": True,
            "comment": "Aggregated validation recall by (definition, model) over full-snapshot validation examples.",
        },
    )
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    critic_image_digest: Mapped[str] = mapped_column(String, primary_key=True)
    critic_model: Mapped[str] = mapped_column(String, primary_key=True)
    recall_stats: Mapped[StatsWithCI | None] = mapped_column(StatsWithCIType(), nullable=True)


class AgentDefinition(Base):
    """The registry proxy writes image definitions on manifest push."""

    __tablename__ = "agent_definitions"
    __table_args__ = (CheckConstraint("is_valid_digest(digest)", name="check_digest_format"),)

    digest: Mapped[str] = mapped_column(String, primary_key=True, comment="OCI image digest (sha256:...)")
    agent_type: Mapped[AgentType] = mapped_column(String, nullable=False, comment="Agent type enum")
    created_by_agent_run_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, index=True, comment="Agent run that created this image (NULL for builtin)"
    )
    base_digest: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="Parent image digest if this is a layered image"
    )
    display_name: Mapped[str | None] = mapped_column(
        String, nullable=True, comment="Human-readable name from OCI label org.opencontainers.image.title"
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    agent_runs: Mapped[list[AgentRun]] = relationship(
        back_populates="agent_definition", foreign_keys="AgentRun.image_digest"
    )


class LLMRequest(Base):
    """Full request/response bodies from the proxy; response token counts feed llm_request_costs."""

    __tablename__ = "llm_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agent_runs.agent_run_id", ondelete="CASCADE"), nullable=False, index=True
    )
    model: Mapped[str] = mapped_column(String, ForeignKey("model_metadata.model_id"), nullable=False, index=True)
    api_shape: Mapped[LLMApiShape] = mapped_column(
        StringBackedStrEnumColumn(LLMApiShape), nullable=False, server_default=LLMApiShape.RESPONSES.value
    )
    request_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())

    agent_run: Mapped[AgentRun] = relationship(back_populates="llm_requests")

    __table_args__ = (CheckConstraint(f"api_shape IN ({_LLM_API_SHAPES_SQL})", name="llm_requests_api_shape_check"),)


class AgentRun(Base):
    """Type-configured run with parent/child spawning.

    The proxy enforces budget_usd across descendants; agent_registry enforces timeout_seconds.
    """

    __tablename__ = "agent_runs"

    agent_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    image_digest: Mapped[str] = mapped_column(
        String,
        ForeignKey("agent_definitions.digest"),
        nullable=False,
        comment="OCI image digest (FK to agent_definitions.digest)",
    )
    parent_agent_run_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agent_runs.agent_run_id"), nullable=True, index=True
    )
    model: Mapped[str] = mapped_column(String, nullable=False)
    type_config: Mapped[TypeConfig] = mapped_column(PydanticColumn(TypeConfig), nullable=False)
    status: Mapped[AgentRunStatus] = mapped_column(
        nullable=False, server_default="in_progress", comment="Terminal status of the agent run"
    )

    budget_usd: Mapped[float] = mapped_column(
        nullable=False, comment="Max USD cost allowed for this agent (including child agents). Enforced by proxy."
    )
    timeout_seconds: Mapped[int | None] = mapped_column(
        nullable=True, comment="Max seconds before agent is killed. Enforced by agent_registry."
    )

    # TODO: Populate started_at when container actually starts executing (currently not set).
    # Could compute deadline as started_at + timeout_seconds for display/monitoring.
    started_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP, nullable=True, comment="When container started executing"
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP, nullable=True, comment="When container finished (success or failure)"
    )
    container_exit_code: Mapped[int | None] = mapped_column(
        nullable=True, comment="Container exit code (NULL if still running or not container-based)"
    )

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    agent_definition: Mapped[AgentDefinition] = relationship(back_populates="agent_runs", foreign_keys=[image_digest])
    parent: Mapped[AgentRun | None] = relationship("AgentRun", remote_side=[agent_run_id], backref="children")
    reported_issues: Mapped[list[ReportedIssue]] = relationship(
        back_populates="agent_run", cascade="all, delete-orphan"
    )
    grading_edges: Mapped[list[GradingEdge]] = relationship(
        back_populates="grader_run", foreign_keys="GradingEdge.grader_run_id", cascade="all, delete-orphan"
    )
    llm_requests: Mapped[list[LLMRequest]] = relationship(back_populates="agent_run", cascade="all, delete-orphan")

    def critic_config(self) -> CriticTypeConfig:
        if isinstance(self.type_config, CriticTypeConfig):
            return self.type_config
        raise ValueError(f"Expected CriticTypeConfig, got {type(self.type_config).__name__}")

    def grader_config(self) -> GraderTypeConfig:
        if isinstance(self.type_config, GraderTypeConfig):
            return self.type_config
        raise ValueError(f"Expected GraderTypeConfig, got {type(self.type_config).__name__}")
