"""Test PydanticColumn behavior with NULL values.

This test demonstrates the footgun: JSONB columns can store JSON null ('null'::jsonb)
which is different from SQL NULL. PydanticColumn deserializes JSON null to Python None,
but SQLAlchemy's .isnot(None) filter only excludes SQL NULL, not JSON null.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from props.core.db.models import PydanticColumn
from props.core.db.session import get_session


class Base(DeclarativeBase):
    pass


class SimpleData(BaseModel):
    """Simple Pydantic model for testing."""

    value: int


class TestTable(Base):
    """Minimal test table with PydanticColumn."""

    __tablename__ = "test_pydantic_null"

    id: Mapped[int] = mapped_column(primary_key=True)
    # This column uses PydanticColumn - same footgun as GraderRun.output
    data: Mapped[SimpleData | None] = mapped_column(PydanticColumn(SimpleData), nullable=True)


@pytest.fixture
def test_pydantic_column_db(test_db):
    """Create test table for PydanticColumn testing.

    Note: test_db fixture already calls init_db(), so we don't call it again.
    We only need to create our specific test table.
    """
    # Create just our test table (don't use recreate_database - that's for props schema)
    with get_session() as session:
        Base.metadata.create_all(bind=session.connection().engine)

    return test_db


def test_sql_null_vs_json_null(test_pydantic_column_db):
    """Demonstrate the difference between SQL NULL and JSON null.

    PROBLEM: Setting data=None creates JSON null ('null'::jsonb), not SQL NULL.
    The filter .isnot(None) only excludes SQL NULL, so JSON null rows pass through.
    When PydanticColumn deserializes them, you get Python None → AttributeError.
    """
    with get_session() as session:
        # Insert row with SQL NULL (skip the column entirely)
        session.execute(text("INSERT INTO test_pydantic_null (id) VALUES (1)"))

        # Insert row with JSON null (explicit NULL value gets serialized to 'null'::jsonb)
        # This simulates what happens in two-phase commit: GraderRun(output=None)
        test_obj = TestTable(id=2, data=None)
        session.add(test_obj)

        # Insert row with actual data
        test_obj_with_data = TestTable(id=3, data=SimpleData(value=42))
        session.add(test_obj_with_data)

        session.commit()

    # Check raw database state
    with get_session() as session:
        result = session.execute(
            text("SELECT id, data IS NULL as is_sql_null, data::text as data_text FROM test_pydantic_null ORDER BY id")
        )
        rows = list(result)

        # Row 1: SQL NULL
        assert rows[0].id == 1
        assert rows[0].is_sql_null is True
        assert rows[0].data_text is None

        # Row 2: JSON null (NOT SQL NULL!)
        assert rows[1].id == 2
        assert rows[1].is_sql_null is False  # ← The footgun!
        assert rows[1].data_text == "null"

        # Row 3: Real data
        assert rows[2].id == 3
        assert rows[2].is_sql_null is False
        assert "42" in rows[2].data_text

    # THE FOOTGUN: .isnot(None) doesn't filter out JSON null
    with get_session() as session:
        # Query with .isnot(None) - should exclude NULL values, right?
        stmt = select(TestTable).where(TestTable.data.isnot(None))
        results = session.execute(stmt).scalars().all()

        # Expected: Only row 3 (with real data)
        # Actual: Rows 2 AND 3 (JSON null passes the filter!)
        assert len(results) == 2
        assert results[0].id == 2
        assert results[0].data is None  # ← PydanticColumn deserialized JSON null to Python None
        assert results[1].id == 3
        assert results[1].data is not None
        assert results[1].data.value == 42

    # FIX: Add explicit JSON null filter
    with get_session() as session:
        stmt = (
            select(TestTable)
            .where(TestTable.data.isnot(None))  # Excludes SQL NULL
            .where(text("data != 'null'::jsonb"))  # Excludes JSON null
        )
        results = session.execute(stmt).scalars().all()

        # Now we correctly get only row 3
        assert len(results) == 1
        assert results[0].id == 3
        assert results[0].data is not None
        assert results[0].data.value == 42


def test_defensive_none_check(test_pydantic_column_db):
    """Show why defensive None checks are needed after query.

    Even with .isnot(None) filter, you can get None values from JSON null.
    Always check result is not None before accessing attributes.
    """
    with get_session() as session:
        # Insert JSON null row
        test_obj = TestTable(id=1, data=None)
        session.add(test_obj)
        session.commit()

    with get_session() as session:
        # Query with .isnot(None) - but JSON null still passes!
        stmt = select(TestTable).where(TestTable.data.isnot(None))
        result = session.execute(stmt).scalar_one()

        # Without defensive check, this would crash:
        # AttributeError: 'NoneType' object has no attribute 'value'
        value = result.data.value if result.data is not None else None

        assert value is None  # JSON null deserialized to Python None


def test_what_does_setting_none_create(test_pydantic_column_db):
    """Clarify what happens when you set a PydanticColumn field to None.

    KEY QUESTION: Does TestTable(data=None) create SQL NULL or JSON null?
    ANSWER: It creates JSON null ('null'::jsonb), NOT SQL NULL.

    This is the source of confusion! When you write:
        obj = GraderRun(output=None)

    You might expect SQL NULL, but PydanticColumn serializes None → 'null'::jsonb.
    """
    with get_session() as session:
        # Set field to None - what gets stored?
        obj = TestTable(id=1, data=None)
        session.add(obj)
        session.commit()

    # Check what's actually in the database
    with get_session() as session:
        result = session.execute(
            text("SELECT id, data IS NULL as is_sql_null, data::text as data_text FROM test_pydantic_null WHERE id = 1")
        ).fetchone()

        assert result is not None
        print("\nWhen you do: TestTable(data=None)")
        print(f"  data IS NULL (SQL NULL): {result.is_sql_null}")
        print(f"  data::text: {result.data_text}")

        # RESULT: SQL NULL = False, data::text = 'null'
        # So TestTable(data=None) creates JSON null, NOT SQL NULL!
        assert result.is_sql_null is False  # Not SQL NULL!
        assert result.data_text == "null"  # JSON null string

    # When you query it back, you get Python None
    with get_session() as session:
        loaded_obj = session.get(TestTable, 1)
        assert loaded_obj is not None
        assert loaded_obj.data is None  # PydanticColumn deserializes JSON null → Python None

        # So the round-trip is:
        # Python None → JSON 'null' → Python None
        # But it's stored as JSON null, not SQL NULL!


def test_how_to_create_sql_null(test_pydantic_column_db):
    """Show how to actually create SQL NULL (skip the column entirely)."""
    with get_session() as session:
        # To create SQL NULL, you must use raw SQL and skip the column
        session.execute(text("INSERT INTO test_pydantic_null (id) VALUES (1)"))
        session.commit()

    with get_session() as session:
        result = session.execute(
            text("SELECT id, data IS NULL as is_sql_null, data::text as data_text FROM test_pydantic_null WHERE id = 1")
        ).fetchone()

        assert result is not None
        print("\nWhen you skip the column entirely:")
        print(f"  data IS NULL (SQL NULL): {result.is_sql_null}")
        print(f"  data::text: {result.data_text}")

        # NOW we have SQL NULL
        assert result.is_sql_null is True  # SQL NULL!
        assert result.data_text is None  # No JSON representation

    # ORM also sees it as None
    with get_session() as session:
        obj = session.get(TestTable, 1)
        assert obj is not None
        assert obj.data is None

        # But this time it's ACTUALLY SQL NULL in the database


def test_proper_fix_with_json_filter(test_pydantic_column_db):
    """Show the proper fix: filter out JSON null at query time."""
    with get_session() as session:
        # Insert various states
        session.execute(text("INSERT INTO test_pydantic_null (id) VALUES (1)"))  # SQL NULL
        session.add(TestTable(id=2, data=None))  # JSON null
        session.add(TestTable(id=3, data=SimpleData(value=42)))  # Real data
        session.commit()

    with get_session() as session:
        # Proper query: exclude both SQL NULL and JSON null
        stmt = (
            select(TestTable)
            .where(TestTable.data.isnot(None))  # SQL NULL
            .where(text("data != 'null'::jsonb"))  # JSON null
        )
        results = session.execute(stmt).scalars().all()

        # Only real data, safe to access .value
        assert len(results) == 1
        assert results[0].data is not None
        assert results[0].data.value == 42  # No AttributeError!
