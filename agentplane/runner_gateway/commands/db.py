"""The Runner Gateway's own schema, in the `sandbox_commands` database: its Session registry.

Session history lives in another database, keyed by the same public Session ID with no foreign key.
"""

from uuid import UUID

from sqlalchemy import String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Session(Base):
    """A public Session in one Sandbox incarnation, which is also the runner's Session ID."""

    __tablename__ = "session"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    sandbox_namespace: Mapped[str] = mapped_column(String)
    sandbox_name: Mapped[str] = mapped_column(String)
    # Absent for imported Sessions whose original Sandbox UID was not retained in app history.
    sandbox_uid: Mapped[UUID | None]
