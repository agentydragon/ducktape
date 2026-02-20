"""SQLAlchemy models for properties evaluation results.

Database schema documentation:
- Setup and architecture: db/README.md
- Access patterns and RLS: AGENTS.md (Database section)
- Migrations: db/migrations/
"""

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

from props.core.agent_types import (
    AgentType,
    CriticDevImproveTypeConfig,
    CriticDevOptimizeTypeConfig,
    CriticTypeConfig,
    FreeformTypeConfig,
    GraderTypeConfig,
    TypeConfig,
)
from props.core.ids import SnapshotSlug, _SnapshotSlugBase
from props.core.models.examples import ExampleKind
from props.core.splits import Split
from props.db.snapshots import LocationAnchor

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

    def scaled(self, divisor: float) -> StatsWithCI:
        """Scale by divisor to convert counts to ratios. Mirrors PostgreSQL scale_stats()."""
        if divisor == 0:
            return StatsWithCI(n=self.n, mean=0.0, min=0.0, max=0.0, lcb95=None, ucb95=None)
        return StatsWithCI(
            n=self.n,
            mean=self.mean / divisor,
            min=self.min / divisor,
            max=self.max / divisor,
            lcb95=self.lcb95 / divisor if self.lcb95 is not None else None,
            ucb95=self.ucb95 / divisor if self.ucb95 is not None else None,
        )


class StatsWithCIType(TypeDecorator[StatsWithCI | None]):
    """SQLAlchemy column type for stats_with_ci PostgreSQL composite type.

    PostgreSQL composite types are returned as named tuples by psycopg2 when
    registered via register_composite(). This type decorator converts between
    named tuples and StatsWithCI Pydantic models.

    Note: We use Text as the impl type because PostgreSQL composite types
    don't have a direct SQLAlchemy type. The actual type coercion is handled
    by register_composite() in database.py.
    """

    # Use Text as a placeholder - the actual type is stats_with_ci in PostgreSQL
    impl = Text
    cache_ok = True

    def process_bind_param(
        self, value: StatsWithCI | None, dialect: Any
    ) -> tuple[int, float, float, float, float | None, float | None] | None:
        """Convert StatsWithCI to tuple for database storage."""
        if value is None:
            return None
        return (value.n, value.mean, value.min, value.max, value.lcb95, value.ucb95)

    def process_result_value(self, value: Any, dialect: Any) -> StatsWithCI | None:
        """Convert named tuple from database to StatsWithCI.

        psycopg2's register_composite() returns a named tuple with attributes
        matching the composite type fields (n, mean, min, max, lcb95, ucb95).
        """
        if value is None:
            return None
        # Access via named attributes (from register_composite's named tuple)
        return StatsWithCI(
            n=value.n, mean=value.mean, min=value.min, max=value.max, lcb95=value.lcb95, ucb95=value.ucb95
        )


class AgentRunStatus(StrEnum):
    """Unified status for all agent types (critic, grader, prompt_optimizer, etc.).

    Terminal states: EXITED (check container_exit_code for success/failure), TIMED_OUT.
    """

    IN_PROGRESS = "in_progress"
    EXITED = "exited"
    TIMED_OUT = "timed_out"


class PydanticColumn[T: BaseModel](TypeDecorator[T]):
    """SQLAlchemy column type that automatically serializes/deserializes any Pydantic model.

    Usage:
        class MyModel(Base):
            data: Mapped[MyPydanticType] = mapped_column(PydanticColumn(MyPydanticType))

    Or register in type_annotation_map for automatic mapping:
        type_annotation_map = {MyPydanticType: PydanticColumn(MyPydanticType)}

    For union types or TypeAliases, pass the type directly (not as a class):
        source: Mapped[Source] = mapped_column(PydanticColumn(Source))

    TODO: Apply this refactor to other JSONB columns in this file where appropriate.
    Candidates: fields that are currently Mapped[dict[str, Any]] but represent
    structured Pydantic models (e.g., input/output fields in CriticRun, GraderRun).
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
    """SQLAlchemy column type for SnapshotSlug.

    Stores as String in DB, validates and wraps as SnapshotSlug on load.
    """

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
        # Validate and wrap in NewType
        validated = self._adapter.validate_python(value)
        return SnapshotSlug(validated)


class PathColumn(TypeDecorator[Path]):
    """SQLAlchemy column type for Path.

    Stores as String in DB, converts to/from Path on load/store.
    """

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


class StrEnumColumn[E: StrEnum](TypeDecorator[E]):
    """Generic SQLAlchemy column type for StrEnum types.

    Uses PostgreSQL ENUM type with values derived from the Python enum.
    Automatically handles conversion between Python enum and database string.

    Usage:
        class MyEnum(StrEnum):
            FOO = "foo"
            BAR = "bar"

        # In type_annotation_map:
        MyEnum: StrEnumColumn(MyEnum, name="my_enum_type")

        # In model:
        my_field: Mapped[MyEnum] = mapped_column()
    """

    impl = Enum
    cache_ok = True

    def __init__(self, enum_class: type[E], name: str):
        self._enum_class = enum_class
        # Derive SQL enum values from Python enum to keep them in sync
        super().__init__(*[e.value for e in enum_class], name=name, create_constraint=True, native_enum=True)

    def process_bind_param(self, value: E | str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return value.value if isinstance(value, self._enum_class) else str(value)

    def process_result_value(self, value: str | None, dialect: Any) -> E | None:
        if value is None:
            return None
        return self._enum_class(value)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[type, Any]] = {
        dict[str, Any]: JSONB,
        UUID: PG_UUID(as_uuid=True),
        SnapshotSlug: SnapshotSlugColumn(),
        Split: StrEnumColumn(Split, name="split_enum"),
        AgentRunStatus: StrEnumColumn(AgentRunStatus, name="agent_run_status_enum"),
    }


class Snapshot(Base):
    """Code snapshot with split assignment.

    Source of truth for snapshot→split mapping.
    Issues/false_positives reference snapshots by slug.
    """

    __tablename__ = "snapshots"

    slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), primary_key=True)
    split: Mapped[Split] = mapped_column(nullable=False)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, comment="tar archive of source code")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
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

    @classmethod
    def get_by_split(cls, split: str) -> list[Snapshot]:
        session = Session.object_session(cls)
        if session is None:
            raise RuntimeError("Model not bound to session")
        return list(session.execute(select(cls).where(cls.split == split)).scalars().all())

    def files_with_issues(self) -> set[Path]:
        tp_files = {
            range_orm.file_path
            for tp in self.true_positives
            for occurrence in tp.occurrences
            for range_orm in occurrence.ranges
        }
        fp_files = {
            range_orm.file_path
            for fp in self.false_positives
            for occurrence in fp.occurrences
            for range_orm in occurrence.ranges
        }
        return tp_files | fp_files


class TruePositive(Base):
    """True positive (expected findings).

    Composite primary key: (snapshot_slug, tp_id).
    Each true positive has one or more occurrences stored in true_positive_occurrences.
    Trigger file sets (critic_scopes_expected_to_recall) are stored in critic_scopes_expected_to_recall M:N table.
    """

    __tablename__ = "true_positives"

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(
        SnapshotSlugColumn(), ForeignKey("snapshots.slug", ondelete="RESTRICT"), primary_key=True
    )
    tp_id: Mapped[str] = mapped_column(String, primary_key=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
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

    @classmethod
    def get_for_snapshot(cls, snapshot_slug: SnapshotSlug) -> list[TruePositive]:
        session = Session.object_session(cls)
        if session is None:
            raise RuntimeError("Model not bound to session")
        return list(session.execute(select(cls).where(cls.snapshot_slug == snapshot_slug)).scalars().all())


class FalsePositive(Base):
    """Known false positive (issue that looks like a problem but isn't).

    Composite primary key: (snapshot_slug, fp_id).
    Each FP has one or more occurrences with relevant_files semantics.
    Occurrences are stored in the separate false_positive_occurrences table.
    """

    __tablename__ = "false_positives"
    __table_args__ = ({"comment": "Patterns the labeler considers acceptable - teaches agents what NOT to flag."},)

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(
        SnapshotSlugColumn(), ForeignKey("snapshots.slug", ondelete="RESTRICT"), primary_key=True
    )
    fp_id: Mapped[str] = mapped_column(String, primary_key=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
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

    @classmethod
    def get_for_snapshot(cls, snapshot_slug: SnapshotSlug) -> list[FalsePositive]:
        session = Session.object_session(cls)
        if session is None:
            raise RuntimeError("Model not bound to session")
        return list(session.execute(select(cls).where(cls.snapshot_slug == snapshot_slug)).scalars().all())


class TruePositiveOccurrenceORM(Base):
    """Occurrence within a true positive issue.

    Each occurrence represents a specific location where the issue manifests.
    critic_scopes_expected_to_recall is stored in the critic_scopes_expected_to_recall M:N table (linking to file_sets).
    File ranges are stored in tp_occurrence_ranges table (normalized from JSONB).
    """

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

    # Relationships
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
        """Derive set of file scopes from critic_scopes_expected_to_recall relationship."""
        result: set[frozenset[Path]] = set()
        for scope in self.critic_scopes_expected_to_recall:
            # Each scope links to a file_set via files_hash
            # Get file paths from file_set_members
            if scope.file_set:
                file_paths = frozenset(Path(m.file_path) for m in scope.file_set.members)
                result.add(file_paths)
        return result


class FalsePositiveOccurrenceORM(Base):
    """Occurrence within a false positive issue.

    Each occurrence represents a specific location where the false positive manifests.
    File ranges are stored in fp_occurrence_ranges table (normalized from JSONB).
    Relevant files are stored in fp_occurrence_relevant_files table (normalized from JSONB).
    """

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

    # Relationships
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
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
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
    """File that makes a false positive occurrence relevant."""

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

    # Relationships
    occurrence: Mapped[FalsePositiveOccurrenceORM] = relationship(back_populates="relevant_file_orms")


class SnapshotFile(Base):
    """File in a snapshot with metadata.

    Used for FK validation of file paths in occurrences and trigger sets.
    Populated during sync from hydrated snapshot content.
    """

    __tablename__ = "snapshot_files"

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(
        SnapshotSlugColumn(), ForeignKey("snapshots.slug", ondelete="CASCADE"), primary_key=True
    )
    file_path: Mapped[str] = mapped_column(
        String, primary_key=True, comment='Path relative to snapshot root (e.g., "src/utils.py"). NOT absolute paths.'
    )
    line_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # Relationships
    snapshot_obj: Mapped[Snapshot] = relationship()


class FileSet(Base):
    """Content-addressable file set for training examples.

    Primary key is (snapshot_slug, files_hash) where files_hash is MD5 of sorted file paths.
    Deduplicated by PK constraint - same files always produce same hash.
    """

    __tablename__ = "file_sets"

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(
        SnapshotSlugColumn(), ForeignKey("snapshots.slug", ondelete="CASCADE"), primary_key=True
    )
    files_hash: Mapped[str] = mapped_column(String, primary_key=True, comment="MD5 of sorted file paths")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())

    # Relationships
    snapshot_obj: Mapped[Snapshot] = relationship()
    members: Mapped[list[FileSetMember]] = relationship(back_populates="file_set", cascade="all")


class FileSetMember(Base):
    """File belonging to a file set.

    FK to snapshot_files validates file paths exist in the snapshot.
    """

    __tablename__ = "file_set_members"

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), primary_key=True)
    files_hash: Mapped[str] = mapped_column(String, primary_key=True)
    file_path: Mapped[str] = mapped_column(String, primary_key=True)

    # Composite FK to file_sets
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

    # Relationships
    file_set: Mapped[FileSet] = relationship(back_populates="members")


class CriticScopeExpectedToRecall(Base):
    """M:N linking TP occurrences to file_sets defining EXPECTED recall scopes.

    DETERMINES: Recall DENOMINATOR only. "From which scopes do we expect critics to find this issue?"
    Each occurrence may have multiple alternative scopes (OR logic: any one suffices).

    DOES NOT CONSTRAIN: Critics CAN find issues outside expected scopes (recall >100% possible).
    A diligent critic reviewing file.py might discover issues in bar.py it depends on.

    DISTINCT FROM match_file_restriction: That field is a HARD constraint on where
    graders can give credit. This field only affects metric denominators.
    """

    __tablename__ = "critic_scopes_expected_to_recall"

    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), primary_key=True)
    tp_id: Mapped[str] = mapped_column(String, primary_key=True)
    occurrence_id: Mapped[str] = mapped_column(String, primary_key=True)
    files_hash: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())

    # Composite FKs
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

    # Relationships
    # overlaps needed because snapshot_slug is in both FKs (to occurrence and file_set)
    file_set: Mapped[FileSet] = relationship(overlaps="critic_scopes_expected_to_recall,occurrence")
    occurrence: Mapped[TruePositiveOccurrenceORM] = relationship(
        back_populates="critic_scopes_expected_to_recall", overlaps="file_set"
    )


class ReportedIssue(Base):
    """Issue reported by an agent during code review.

    Part of the critic workflow - agent creates issue headers and links occurrences.
    Uses compound primary key (agent_run_id, issue_id) for scoped uniqueness.
    Agent uses hard DELETE to remove incorrect issues.
    """

    __tablename__ = "reported_issues"

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

    # Relationships
    agent_run: Mapped[AgentRun] = relationship(back_populates="reported_issues")
    occurrences: Mapped[list[ReportedIssueOccurrence]] = relationship(
        back_populates="reported_issue", cascade="all, delete-orphan"
    )


class ReportedIssueOccurrence(Base):
    """Specific location(s) for a reported issue.

    Each occurrence has 1+ locations (JSONB array).
    Each location: {file: str, start_line?: int, end_line?: int}

    Example:
    locations = [
        {"file": "src/foo.py", "start_line": 10, "end_line": 20},
        {"file": "src/bar.py", "start_line": 30, "end_line": 40}
    ]

    CHECK constraint ensures locations is non-empty array.
    """

    __tablename__ = "reported_issue_occurrences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, server_default=FetchedValue())
    reported_issue_id: Mapped[str] = mapped_column(String, nullable=False)

    locations: Mapped[list[LocationAnchor]] = mapped_column(
        PydanticColumn(list[LocationAnchor]),
        nullable=False,
        comment="1+ location anchors: {file, start_line?, end_line?}",
    )

    # Audit trail
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())
    cancelled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Foreign key to composite primary key
    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_run_id", "reported_issue_id"],
            ["reported_issues.agent_run_id", "reported_issues.issue_id"],
            ondelete="CASCADE",
        ),
    )

    # Relationships
    reported_issue: Mapped[ReportedIssue] = relationship(back_populates="occurrences")


class GradingPending(Base):
    """View: missing grading edges (drift detection).

    Each row represents a (critique_issue, gt_occurrence) pair that needs grading.
    When this view returns no rows for a grader's scope, grading is complete.
    """

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


# --- Grading Target Types (Discriminated Union) ---


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
    """Explicit bipartite graph edge from critique issue to GT occurrence.

    Each edge represents a grader's judgment about whether a critique issue matches
    a ground truth occurrence. Every (critique_issue, gt_occurrence) pair must have
    an edge (complete bipartite coverage enforced by grading_pending view).

    Exactly one of TP or FP target must be set (enforced by DB CHECK constraint):
    - TP edge: tp_id + tp_occurrence_id NOT NULL, fp_id + fp_occurrence_id NULL
    - FP edge: fp_id + fp_occurrence_id NOT NULL, tp_id + tp_occurrence_id NULL

    Credit semantics:
    - For TPs: 0.0-1.0 (how well critique matches; 0.0 = reviewed, no match)
    - For FPs: 0.0 for non-match, >0 for penalty (incorrectly triggered FP)

    Most edges have credit=0.0 (sparse matches). Query grading_pending view to see
    missing edges that still need grading.
    """

    __tablename__ = "grading_edges"
    __table_args__ = ({"comment": "Bipartite graph edges from critique issues to GT occurrences."},)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Critique reference
    critique_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    critique_issue_id: Mapped[str] = mapped_column(String, nullable=False)

    # Snapshot (for FK validation)
    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), nullable=False)

    # TP target (nullable - exactly one of TP or FP)
    tp_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tp_occurrence_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # FP target (nullable)
    fp_id: Mapped[str | None] = mapped_column(String, nullable=True)
    fp_occurrence_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Grading metadata
    credit: Mapped[float] = mapped_column(nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    grader_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agent_runs.agent_run_id", ondelete="CASCADE"), nullable=False
    )

    # Audit trail
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())

    # Relationships
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
    """Cluster of unmatched critique issues that report the same underlying problem.

    Groups issues from different critic runs that describe the same novel finding
    (one not covered by ground truth TPs/FPs). Only issues with all grading edges
    at credit=0 can be clustered.

    Composite primary key: (snapshot_slug, cluster_id).
    """

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

    # Relationships
    members: Mapped[list[IssueClusterMember]] = relationship(back_populates="cluster", cascade="all, delete-orphan")


class IssueClusterMember(Base):
    """Membership of a critique issue in a cluster.

    Each critique issue belongs to at most one cluster (UNIQUE constraint).
    Only issues with no positive grading edges (credit > 0) can be members.
    """

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

    # Relationships
    cluster: Mapped[IssueCluster] = relationship(back_populates="members")


class ClusteringPending(Base):
    """View: critique issues that need clustering (fully graded, no positive match, not yet clustered).

    When this view returns no rows for a snapshot, all unmatched issues are clustered.
    """

    __tablename__ = "clustering_pending"
    __table_args__ = ({"info": {"is_view": True}},)

    critique_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    critique_issue_id: Mapped[str] = mapped_column(String, primary_key=True)
    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn())


class ModelMetadata(Base):
    """Model metadata: pricing, context limits, and upstream routing.

    Synchronized from:
    - openai_utils.model_metadata.MODEL_METADATA (OpenAI models)
    - PropsConfig.models (custom/local models)

    Upstream routing:
    - upstream_name: References [upstreams.*] in props config. NULL = "openai" default.
    - upstream_model: Model name to send in API request. NULL = use model_id.

    Enables post-hoc cost calculation, context validation, and multi-upstream routing.
    """

    __tablename__ = "model_metadata"

    model_id: Mapped[str] = mapped_column(String, primary_key=True)
    input_usd_per_1m_tokens: Mapped[float] = mapped_column(nullable=False)
    cached_input_usd_per_1m_tokens: Mapped[float] = mapped_column(nullable=False)
    output_usd_per_1m_tokens: Mapped[float] = mapped_column(nullable=False)
    context_window_tokens: Mapped[int] = mapped_column(nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(nullable=False)
    upstream_name: Mapped[str | None] = mapped_column(String, nullable=True)
    upstream_model: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class LLMRunCost(Base):
    """Aggregated LLM costs per agent run from llm_run_costs database VIEW.

    Aggregates token usage and costs per (agent_run_id, model) from llm_requests
    logged by the proxy.
    """

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
    """Per-agent-run budget status from agent_run_budget_status VIEW.

    own_spent_usd: direct LLM costs for this agent only.
    tree_spent_usd: recursive subtree costs (this agent + all descendants).
    remaining_usd: budget_usd - tree_spent_usd.
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
    """Per-(critique_run, tp, occurrence) credit from tp_occurrence_credits VIEW.

    One row per (critic_run, tp_id, occurrence_id). found_credit is the SUM of all
    grading edge credits for that combination (≤ 1.0 enforced by DB trigger).
    grader_run_id is not exposed — it is provenance, not an aggregation dimension.
    """

    __tablename__ = "tp_occurrence_credits"
    __table_args__ = {"info": {"is_view": True}, "extend_existing": True}  # noqa: RUF012
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    # Composite primary key (critic_run_id, tp_id, occurrence_id)
    critic_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tp_id: Mapped[str] = mapped_column(String, primary_key=True)
    occurrence_id: Mapped[str] = mapped_column(String, primary_key=True)

    # Snapshot/Example context
    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), nullable=False)
    split: Mapped[Split] = mapped_column(nullable=False)
    example_kind: Mapped[ExampleKind] = mapped_column(EXAMPLE_KIND_ENUM_TYPE, nullable=False)
    files_hash: Mapped[str | None] = mapped_column(String, nullable=True)  # NULL for whole_snapshot

    # Critic provenance
    critic_image_digest: Mapped[str] = mapped_column(String, nullable=False)
    critic_model: Mapped[str] = mapped_column(String, nullable=False)

    # Credit (sum of all grading edges for this critique+occurrence, ≤ 1.0)
    found_credit: Mapped[float] = mapped_column(nullable=False)


class RecallByRun(Base):
    """Per-critic-run recall from recall_by_run database VIEW.

    Includes all runs (in_progress, exited, timed_out). missing_grading_edges counts
    how many grading_pending edges remain for this run (0 = grading complete).
    """

    __tablename__ = "recall_by_run"
    __table_args__ = {"info": {"is_view": True}, "extend_existing": True}  # noqa: RUF012
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    # Primary key
    critic_run_id: Mapped[UUID] = mapped_column(primary_key=True)

    # Example identification
    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), nullable=False)
    example_kind: Mapped[ExampleKind] = mapped_column(EXAMPLE_KIND_ENUM_TYPE, nullable=False)
    files_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    split: Mapped[Split] = mapped_column(nullable=False)
    recall_denominator: Mapped[int] = mapped_column(Integer, nullable=False)

    # Critic-specific columns
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
    """Per-(definition, model, example) recall statistics from recall_by_definition_example VIEW.

    Intermediate view between recall_by_run and higher-level aggregations.
    Groups recall_by_run by (definition, model, example) - used by GEPA.

    - recall_denominator: Ground truth count (denominator)
    - credit_stats: Stats of raw credit counts across runs (numerator)
    - recall_stats: credit_stats / recall_denominator
    """

    __tablename__ = "recall_by_definition_example"
    __table_args__ = {"info": {"is_view": True}, "extend_existing": True}  # noqa: RUF012
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    # Composite primary key
    critic_image_digest: Mapped[str] = mapped_column(String, primary_key=True)
    critic_model: Mapped[str] = mapped_column(String, primary_key=True)
    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), primary_key=True)
    example_kind: Mapped[ExampleKind] = mapped_column(EXAMPLE_KIND_ENUM_TYPE, primary_key=True)
    files_hash: Mapped[str | None] = mapped_column(String, primary_key=True)
    split: Mapped[Split] = mapped_column(nullable=False)

    # Ground truth count (denominator)
    recall_denominator: Mapped[int] = mapped_column(Integer, nullable=False)

    # Number of critic runs for this (definition, model, example)
    n_runs: Mapped[int] = mapped_column(Integer, nullable=False)

    # Status breakdown (JSONB: {AgentRunStatus.EXITED: 5, ...})
    status_counts: Mapped[dict[AgentRunStatus, int]] = mapped_column(JSONB, nullable=False)

    # Credit stats (numerator; failed runs count as 0 credit)
    credit_stats: Mapped[StatsWithCI | None] = mapped_column(StatsWithCIType(), nullable=True)

    # Recall statistics (credit_stats / recall_denominator)
    recall_stats: Mapped[StatsWithCI | None] = mapped_column(StatsWithCIType(), nullable=True)


class RecallByDefinitionSplitKind(Base):
    """Recall by (definition, model, split, example_kind) from recall_by_definition_split_kind VIEW.

    Aggregates recall_by_definition_example across examples within each (split, example_kind) group.

    - recall_denominator: Sum across distinct examples (denominator)
    - credit_stats: Stats of raw credit counts across runs (numerator)
    - recall_stats: credit_stats / recall_denominator
    """

    __tablename__ = "recall_by_definition_split_kind"
    __table_args__ = {"info": {"is_view": True}, "extend_existing": True}  # noqa: RUF012
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    # Composite primary key (matches view GROUP BY)
    split: Mapped[Split] = mapped_column(primary_key=True)
    example_kind: Mapped[ExampleKind] = mapped_column(EXAMPLE_KIND_ENUM_TYPE, primary_key=True)
    critic_image_digest: Mapped[str] = mapped_column(String, primary_key=True)
    critic_model: Mapped[str] = mapped_column(String, primary_key=True)

    # Example and run counts
    n_examples: Mapped[int] = mapped_column(Integer, nullable=False)
    n_runs: Mapped[int] = mapped_column(Integer, nullable=False)

    # Catchable occurrences (denominator - sum across distinct examples)
    recall_denominator: Mapped[int] = mapped_column(Integer, nullable=False)

    # Status breakdown (JSONB: {AgentRunStatus.EXITED: 5, ...})
    status_counts: Mapped[dict[AgentRunStatus, int]] = mapped_column(JSONB, nullable=False)

    # Credit stats (numerator; failed runs count as 0 credit)
    credit_stats: Mapped[StatsWithCI | None] = mapped_column(StatsWithCIType(), nullable=True)

    # Recall statistics (credit_stats / recall_denominator)
    recall_stats: Mapped[StatsWithCI | None] = mapped_column(StatsWithCIType(), nullable=True)

    # Count of runs where caught credit was exactly 0 (complete failure to find anything)
    zero_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class RecallByExample(Base):
    """Per-example recall statistics from recall_by_example database VIEW.

    Aggregates recall_by_definition_example across definitions.

    - recall_denominator: Ground truth count (denominator)
    - credit_stats: Stats of raw credit counts across runs (numerator)
    - recall_stats: credit_stats / recall_denominator
    """

    __tablename__ = "recall_by_example"
    __table_args__ = {"info": {"is_view": True}, "extend_existing": True}  # noqa: RUF012
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    # Composite primary key
    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), primary_key=True)
    example_kind: Mapped[ExampleKind] = mapped_column(EXAMPLE_KIND_ENUM_TYPE, primary_key=True)
    files_hash: Mapped[str | None] = mapped_column(String, primary_key=True)
    split: Mapped[Split] = mapped_column(primary_key=True)
    critic_model: Mapped[str] = mapped_column(String, primary_key=True)

    # Catchable occurrences (denominator)
    recall_denominator: Mapped[int] = mapped_column(Integer, nullable=False)

    # Run count
    n_runs: Mapped[int] = mapped_column(Integer, nullable=False)

    # Status breakdown (JSONB: {AgentRunStatus.EXITED: 5, ...})
    status_counts: Mapped[dict[AgentRunStatus, int]] = mapped_column(JSONB, nullable=False)

    # Credit stats (numerator; failed runs count as 0 credit)
    credit_stats: Mapped[StatsWithCI | None] = mapped_column(StatsWithCIType(), nullable=True)

    # Recall statistics (credit_stats / recall_denominator)
    recall_stats: Mapped[StatsWithCI | None] = mapped_column(StatsWithCIType(), nullable=True)


class WinningDefinition(BaseModel):
    image_digest: str
    credit_stats: StatsWithCI
    n_runs: int


_WinningDefinitionListAdapter: TypeAdapter[list[WinningDefinition]] = TypeAdapter(list[WinningDefinition])


class ParetoFrontierByExample(Base):
    """Pareto frontier from pareto_frontier_by_example database VIEW (not a table).

    For each example, shows definitions that achieved the best mean credit.

    For each (snapshot_slug, split, example_kind, files_hash, critic_model), shows:
    - recall_denominator: ground truth count (denominator for recall)
    - winning_definitions: list of {image_digest, credit_stats, n_runs} for all definitions at best score

    All entries in winning_definitions have the same credit_stats.mean (the best score).
    Consumer can compute recall as best_mean_credit / recall_denominator.

    Useful for prompt optimization to identify:
    - Which definitions excel on specific examples (definition specialization)
    - Examples where no definition performs well (improvement opportunities)
    - Generalist vs specialist definition patterns

    Built on recall_by_definition_example, which aggregates over runs.
    Failed critic runs (timed_out) count as 0.0 credit.
    """

    __tablename__ = "pareto_frontier_by_example"
    __table_args__ = {"info": {"is_view": True}, "extend_existing": True}  # noqa: RUF012
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    # Composite primary key
    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), primary_key=True)
    split: Mapped[Split] = mapped_column(primary_key=True)
    example_kind: Mapped[ExampleKind] = mapped_column(EXAMPLE_KIND_ENUM_TYPE, primary_key=True)
    files_hash: Mapped[str | None] = mapped_column(String, primary_key=True)
    critic_model: Mapped[str] = mapped_column(String, primary_key=True)

    # Ground truth count for this example
    recall_denominator: Mapped[int] = mapped_column(Integer, nullable=False)

    # JSONB array of {image_digest, credit_stats, n_runs} objects
    _winning_definitions_raw: Mapped[list[dict[str, Any]]] = mapped_column("winning_definitions", JSONB, nullable=False)

    @property
    def winning_definitions(self) -> list[WinningDefinition]:
        return _WinningDefinitionListAdapter.validate_python(self._winning_definitions_raw)

    @property
    def winning_image_digests(self) -> list[str]:
        return [w.image_digest for w in self.winning_definitions]

    @property
    def best_mean_credit(self) -> float:
        return self.winning_definitions[0].credit_stats.mean


class OccurrenceStatistics(Base):
    """Per-occurrence statistics across critic runs from occurrence_statistics VIEW.

    Aggregated credit statistics per occurrence. credit_stats.n = number of critic runs,
    credit_stats.mean = average credit across runs.

    Use cases:
    - Identify "hard" occurrences (low credit_stats.mean, high variance)
    - Training diagnostics: "Which occurrences are never caught?"
    - Prompt improver: "Focus on occurrences with low credit_stats.mean"
    """

    __tablename__ = "occurrence_statistics"
    __table_args__ = {"info": {"is_view": True}, "extend_existing": True}  # noqa: RUF012
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    # Example identification
    snapshot_slug: Mapped[SnapshotSlug] = mapped_column(SnapshotSlugColumn(), primary_key=True)
    split: Mapped[Split] = mapped_column(primary_key=True)
    example_kind: Mapped[ExampleKind] = mapped_column(EXAMPLE_KIND_ENUM_TYPE, primary_key=True)
    files_hash: Mapped[str | None] = mapped_column(String, primary_key=True)

    # Ground truth identification
    tp_id: Mapped[str] = mapped_column(String, primary_key=True)
    occurrence_id: Mapped[str] = mapped_column(String, primary_key=True)

    # Critic-specific
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
    """Agent image definition stored as OCI digest.

    The registry proxy writes rows to this table on manifest push.
    Digest is the primary key (sha256:...).
    """

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
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    agent_runs: Mapped[list[AgentRun]] = relationship(
        back_populates="agent_definition", foreign_keys="AgentRun.image_digest"
    )


class LLMRequest(Base):
    """LLM API request logged by the proxy.

    Records all requests made through the LLM proxy, including full request/response
    payloads for debugging. Token counts are extracted from the response at log time
    and used by the llm_request_costs view for cost calculation.
    """

    __tablename__ = "llm_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agent_runs.agent_run_id", ondelete="CASCADE"), nullable=False, index=True
    )
    model: Mapped[str] = mapped_column(String, ForeignKey("model_metadata.model_id"), nullable=False, index=True)
    request_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.now())

    # Relationships
    agent_run: Mapped[AgentRun] = relationship(back_populates="llm_requests")


class AgentRun(Base):
    """Unified agent run record (replaces separate critic_runs, grader_runs, etc.).

    Each run references an agent definition and stores type-specific config as JSONB.
    Parent-child relationships track sub-agent spawning.

    Status tracking:
    - status: Current run status (in_progress, completed, etc.)
    - container_exit_code: Exit code from container (NULL if still running)

    Resource limits (set at launch time):
    - budget_usd: Max USD cost allowed (including child agents). Enforced by proxy.
    - timeout_seconds: Max seconds before agent is killed. Enforced by agent_registry.

    Container lifecycle timestamps:
    - started_at: When container started executing
    - ended_at: When container finished (success or failure)

    Image reference:
    - image_digest: OCI image digest (sha256:...), FK to agent_definitions.digest
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

    # Resource limits (set at launch time)
    budget_usd: Mapped[float] = mapped_column(
        nullable=False, comment="Max USD cost allowed for this agent (including child agents). Enforced by proxy."
    )
    timeout_seconds: Mapped[int | None] = mapped_column(
        nullable=True, comment="Max seconds before agent is killed. Enforced by agent_registry."
    )

    # Container lifecycle timestamps
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
    # Container logs captured after container exits (for in-container agent loops)
    container_stdout: Mapped[str | None] = mapped_column(Text, nullable=True)
    container_stderr: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    agent_definition: Mapped[AgentDefinition] = relationship(back_populates="agent_runs", foreign_keys=[image_digest])
    parent: Mapped[AgentRun | None] = relationship("AgentRun", remote_side=[agent_run_id], backref="children")
    reported_issues: Mapped[list[ReportedIssue]] = relationship(
        back_populates="agent_run", cascade="all, delete-orphan"
    )
    grading_edges: Mapped[list[GradingEdge]] = relationship(
        back_populates="grader_run", foreign_keys="GradingEdge.grader_run_id", cascade="all, delete-orphan"
    )
    llm_requests: Mapped[list[LLMRequest]] = relationship(back_populates="agent_run", cascade="all, delete-orphan")

    # Type-safe config accessors
    def critic_config(self) -> CriticTypeConfig:
        if isinstance(self.type_config, CriticTypeConfig):
            return self.type_config
        raise ValueError(f"Expected CriticTypeConfig, got {type(self.type_config).__name__}")

    def grader_config(self) -> GraderTypeConfig:
        if isinstance(self.type_config, GraderTypeConfig):
            return self.type_config
        raise ValueError(f"Expected GraderTypeConfig, got {type(self.type_config).__name__}")

    def critic_dev_improve_config(self) -> CriticDevImproveTypeConfig:
        if isinstance(self.type_config, CriticDevImproveTypeConfig):
            return self.type_config
        raise ValueError(f"Expected CriticDevImproveTypeConfig, got {type(self.type_config).__name__}")

    def critic_dev_optimize_config(self) -> CriticDevOptimizeTypeConfig:
        if isinstance(self.type_config, CriticDevOptimizeTypeConfig):
            return self.type_config
        raise ValueError(f"Expected CriticDevOptimizeTypeConfig, got {type(self.type_config).__name__}")

    def freeform_config(self) -> FreeformTypeConfig:
        if isinstance(self.type_config, FreeformTypeConfig):
            return self.type_config
        raise ValueError(f"Expected FreeformTypeConfig, got {type(self.type_config).__name__}")
