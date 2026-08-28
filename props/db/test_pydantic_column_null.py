"""PydanticColumn NULL-handling footgun."""

from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from props.db.database import Database
from props.db.models import PydanticColumn


class Base(DeclarativeBase):
    pass


class SimpleData(BaseModel):
    value: int


class PydanticNullTable(Base):
    __tablename__ = "test_pydantic_null"

    id: Mapped[int] = mapped_column(primary_key=True)
    data: Mapped[SimpleData | None] = mapped_column(PydanticColumn(SimpleData), nullable=True)


@pytest.fixture
def test_pydantic_column_db(db: Database):
    with db.session() as session:
        Base.metadata.create_all(bind=session.connection().engine)
    return db


def test_sql_null_vs_json_null(test_pydantic_column_db):
    """Footgun: PydanticColumn stores Python None as ``'null'::jsonb``, not SQL NULL, and
    ``.isnot(None)`` only excludes SQL NULL — JSON-null rows pass the filter and come back
    as Python None. Production queries over nullable PydanticColumn fields must respect
    this: add an explicit ``data != 'null'::jsonb`` predicate (or a None check on results).
    """
    db = test_pydantic_column_db
    with db.session() as session:
        session.execute(text("INSERT INTO test_pydantic_null (id) VALUES (1)"))  # SQL NULL
        session.add(PydanticNullTable(id=2, data=None))  # stored as JSON null
        session.add(PydanticNullTable(id=3, data=SimpleData(value=42)))
        session.commit()

    with db.session() as session:
        rows = list(
            session.execute(
                text(
                    "SELECT id, data IS NULL as is_sql_null, data::text as data_text FROM test_pydantic_null ORDER BY id"
                )
            )
        )
        assert (rows[0].is_sql_null, rows[0].data_text) == (True, None)
        # data=None persisted as JSON null, not SQL NULL:
        assert (rows[1].is_sql_null, rows[1].data_text) == (False, "null")
        assert rows[2].is_sql_null is False

    with db.session() as session:
        # .isnot(None) does not filter the JSON-null row; it deserializes to Python None.
        results = (
            session.execute(
                select(PydanticNullTable).where(PydanticNullTable.data.isnot(None)).order_by(PydanticNullTable.id)
            )
            .scalars()
            .all()
        )
        assert [(r.id, r.data) for r in results] == [(2, None), (3, SimpleData(value=42))]

    with db.session() as session:
        # The explicit JSON-null predicate is what actually excludes those rows.
        results = (
            session.execute(
                select(PydanticNullTable).where(PydanticNullTable.data.isnot(None)).where(text("data != 'null'::jsonb"))
            )
            .scalars()
            .all()
        )
        assert [(r.id, r.data) for r in results] == [(3, SimpleData(value=42))]


if __name__ == "__main__":
    pytest_bazel.main()
