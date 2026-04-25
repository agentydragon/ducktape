"""SQLAlchemy models for the Y.Doc-backed state store.

One row, in one table: the latest binary Y-CRDT update for the
canonical doc. There is no event log table — Y-CRDT itself stores all
the operational history inside the binary blob, and we never need to
query for "what changed when" beyond what the doc already exposes via
its ops.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Integer, LargeBinary
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DocRow(Base):
    """The single canonical Y.Doc, serialized as a binary update blob."""

    __tablename__ = "doc"
    __table_args__ = (CheckConstraint("id = 1", name="doc_single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    update_blob: Mapped[bytes] = mapped_column(LargeBinary)
