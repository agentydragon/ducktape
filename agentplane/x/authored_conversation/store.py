"""The persisted conversation, independently of the model's working context."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, String, Uuid, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class Author(StrEnum):
    HUMAN = "human"
    AGENT = "agent"


class Message(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    position: int
    author: Author
    body: str


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
    body: Mapped[str] = mapped_column(String)


class Store:
    def __init__(self, engine: Engine, *, max_message_chars: int) -> None:
        self.engine = engine
        self.max_message_chars = max_message_chars

    def append(self, author: Author, body: str) -> Message:
        if not body.strip() or len(body) > self.max_message_chars:
            raise ValueError(f"message must contain 1..{self.max_message_chars} characters")
        with Session(self.engine) as session, session.begin():
            row = MessageRow(id=uuid4(), author=author, body=body)
            session.add(row)
            session.flush()
            return Message.model_validate(row)

    def messages(self, *, after_position: int = 0, author: Author | None = None) -> list[Message]:
        query = select(MessageRow).where(MessageRow.position > after_position).order_by(MessageRow.position)
        if author is not None:
            query = query.where(MessageRow.author == author)
        with Session(self.engine) as session:
            return [Message.model_validate(row) for row in session.scalars(query)]

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
                session.scalars(
                    select(MessageRow).where(MessageRow.position > after).order_by(MessageRow.position).limit(limit + 1)
                )
            )
            messages = [Message.model_validate(row) for row in rows[:limit]]
            return Page(messages=messages, next_after_message_id=messages[-1].id if len(rows) > limit else None)


@contextmanager
def open_store(path: Path, *, max_message_chars: int) -> Iterator[Store]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        engine = create_engine(f"sqlite:///{path}")
        stack.callback(engine.dispose)
        Base.metadata.create_all(engine)
        yield Store(engine, max_message_chars=max_message_chars)
