"""The persisted conversation, independently of the model's working context."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, ForeignKey, Select, String, UniqueConstraint, Uuid, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from agentplane.x.authored_conversation.migrations import migrate


class Author(StrEnum):
    HUMAN = "human"
    AGENT = "agent"


class Message(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    position: int
    author: Author
    body: str
    revision: int
    change_sequence: int
    recorded_at: datetime


class Page(BaseModel):
    messages: list[Message]
    next_after_message_id: UUID | None


class Base(DeclarativeBase):
    pass


class MessageRow(Base):
    __tablename__ = "message"

    position: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    author: Mapped[str] = mapped_column(String)


class RevisionRow(Base):
    __tablename__ = "revision"
    __table_args__ = (UniqueConstraint("message_id", "revision"), {"sqlite_autoincrement": True})

    change_sequence: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    message_id: Mapped[UUID] = mapped_column(ForeignKey("message.id"))
    revision: Mapped[int]
    body: Mapped[str] = mapped_column(String)
    recorded_at: Mapped[str] = mapped_column(String)


def message_value(header: MessageRow, revision: RevisionRow) -> Message:
    return Message(
        id=header.id,
        position=header.position,
        author=Author(header.author),
        body=revision.body,
        revision=revision.revision,
        change_sequence=revision.change_sequence,
        recorded_at=datetime.fromisoformat(revision.recorded_at),
    )


def latest_messages() -> Select[tuple[MessageRow, RevisionRow]]:
    return (
        select(MessageRow, RevisionRow)
        .join(RevisionRow, RevisionRow.message_id == MessageRow.id)
        .where(
            RevisionRow.change_sequence.in_(
                select(func.max(RevisionRow.change_sequence)).group_by(RevisionRow.message_id)
            )
        )
    )


class Store:
    def __init__(self, engine: Engine, *, max_message_chars: int) -> None:
        self.engine = engine
        self.max_message_chars = max_message_chars

    def append(self, author: Author, body: str) -> Message:
        self.validate_body(body)
        with Session(self.engine) as session, session.begin():
            row = MessageRow(id=uuid4(), author=author)
            session.add(row)
            session.flush()
            return self.add_revision(session, row, body, revision=1)

    def validate_body(self, body: str) -> None:
        if not body.strip() or len(body) > self.max_message_chars:
            raise ValueError(f"message must contain 1..{self.max_message_chars} characters")

    def add_revision(self, session: Session, header: MessageRow, body: str, *, revision: int) -> Message:
        row = RevisionRow(message_id=header.id, revision=revision, body=body, recorded_at=datetime.now(UTC).isoformat())
        session.add(row)
        session.flush()
        return message_value(header, row)

    def edit(self, message_id: UUID, *, author: Author, expected_revision: int, body: str) -> Message:
        self.validate_body(body)
        with Session(self.engine) as session, session.begin():
            # Serialize the version check and insert across independent SQLite connections.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = session.execute(latest_messages().where(MessageRow.id == message_id)).one_or_none()
            if row is None:
                raise ValueError("unknown message")
            header, current = row
            if header.author != author:
                raise ValueError("participants can only edit their own messages")
            if current.revision != expected_revision:
                raise ValueError(
                    f"revision conflict: expected {expected_revision}, current {current.revision}; reread first"
                )
            return self.add_revision(session, header, body, revision=current.revision + 1)

    def get(self, message_id: UUID, *, revision: int | None = None) -> Message:
        query = (
            latest_messages()
            if revision is None
            else select(MessageRow, RevisionRow)
            .join(RevisionRow, RevisionRow.message_id == MessageRow.id)
            .where(RevisionRow.revision == revision)
        )
        with Session(self.engine) as session:
            row = session.execute(query.where(MessageRow.id == message_id)).one_or_none()
            if row is None:
                raise ValueError("unknown message or revision")
            return message_value(*row)

    def revisions(self, message_id: UUID) -> list[Message]:
        self.get(message_id)
        with Session(self.engine) as session:
            return [
                message_value(*row)
                for row in session.execute(
                    select(MessageRow, RevisionRow)
                    .join(RevisionRow, RevisionRow.message_id == MessageRow.id)
                    .where(MessageRow.id == message_id)
                    .order_by(RevisionRow.revision)
                )
            ]

    def changes(self, *, after_sequence: int = 0, author: Author | None = None) -> list[Message]:
        query = (
            select(MessageRow, RevisionRow)
            .join(RevisionRow, RevisionRow.message_id == MessageRow.id)
            .where(RevisionRow.change_sequence > after_sequence)
            .order_by(RevisionRow.change_sequence)
        )
        if author is not None:
            query = query.where(MessageRow.author == author)
        with Session(self.engine) as session:
            return [message_value(*row) for row in session.execute(query)]

    def messages(self, *, after_position: int = 0, author: Author | None = None) -> list[Message]:
        query = latest_messages().where(MessageRow.position > after_position).order_by(MessageRow.position)
        if author is not None:
            query = query.where(MessageRow.author == author)
        with Session(self.engine) as session:
            return [message_value(*row) for row in session.execute(query)]

    def read(self, *, after_message_id: UUID | None, limit: int) -> Page:
        if limit < 1:
            raise ValueError("page limit must be positive")
        with Session(self.engine) as session:
            after = 0
            if after_message_id is not None:
                cursor = session.scalar(select(MessageRow.position).where(MessageRow.id == after_message_id))
                if cursor is None:
                    raise ValueError("unknown message cursor")
                after = cursor
            rows = list(
                session.execute(
                    latest_messages().where(MessageRow.position > after).order_by(MessageRow.position).limit(limit + 1)
                )
            )
            messages = [message_value(*row) for row in rows[:limit]]
            return Page(messages=messages, next_after_message_id=messages[-1].id if len(rows) > limit else None)


@contextmanager
def open_store(path: Path, *, max_message_chars: int) -> Iterator[Store]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        engine = create_engine(f"sqlite:///{path}")
        stack.callback(engine.dispose)
        migrate(engine)
        yield Store(engine, max_message_chars=max_message_chars)
