"""Job/result persistence (dispatcher database in haku-dispatch-db).

Schema is created with create_all at startup — this service owns its database
exclusively, and the schema is additive-only for now; first breaking change
brings Alembic (the airlock pattern).

Haku reads these tables directly with the read-only `haku_reader` CNPG managed
role (member of pg_read_all_data) — no read API and no grant management here.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from haku.dispatch.models import JobRecord, JobStatus

SessionMaker = async_sessionmaker[AsyncSession]


def _str_enum(enum_type: type) -> Enum:
    # Store StrEnum values (not member names), as a plain VARCHAR: readable
    # from haku_reader's raw SQL and no Postgres-type migrations on new values.
    return Enum(enum_type, native_enum=False, length=16, values_callable=lambda e: [m.value for m in e])


class Base(DeclarativeBase):
    pass


class JobRow(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(63), primary_key=True)
    zone: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(100))
    prompt: Mapped[str] = mapped_column(Text)
    status: Mapped[JobStatus] = mapped_column(_str_enum(JobStatus), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    result: Mapped["ResultRow | None"] = relationship(back_populates="job", lazy="joined")


class ResultRow(Base):
    __tablename__ = "results"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    result: Mapped[str] = mapped_column(Text)
    exit_code: Mapped[int] = mapped_column(Integer)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    job: Mapped[JobRow] = relationship(back_populates="result")


def to_record(row: JobRow) -> JobRecord:
    return JobRecord(
        id=row.id,
        zone=row.zone,
        model=row.model,
        status=row.status,
        prompt=row.prompt,
        created_at=row.created_at,
        completed_at=row.result.submitted_at if row.result else None,
        exit_code=row.result.exit_code if row.result else None,
        result=row.result.result if row.result else None,
    )


def make_engine(database_url: str) -> AsyncEngine:
    # CNPG secrets carry postgresql://; SQLAlchemy async needs the asyncpg driver.
    url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(url)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def insert_job(session: AsyncSession, record: JobRecord) -> None:
    session.add(
        JobRow(
            id=record.id,
            zone=record.zone,
            model=record.model,
            prompt=record.prompt,
            status=record.status,
            created_at=record.created_at,
        )
    )
    await session.commit()


async def get_job(session: AsyncSession, job_id: str) -> JobRow | None:
    return await session.get(JobRow, job_id)


async def store_result(session: AsyncSession, job: JobRow, result: str, exit_code: int) -> None:
    job.result = ResultRow(result=result, exit_code=exit_code, submitted_at=datetime.now(UTC))
    job.status = JobStatus.COMPLETED if exit_code == 0 else JobStatus.FAILED
    await session.commit()


async def mark_killed(session: AsyncSession, job: JobRow) -> None:
    job.status = JobStatus.KILLED
    await session.commit()
