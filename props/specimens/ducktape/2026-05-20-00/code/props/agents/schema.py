"""SQLAlchemy model schema description for agent prompts.

Describes database tables and views by introspecting SQLAlchemy model metadata.
No database connection needed — works from compiled-in model classes.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import postgresql

from props.db.models import Base

_PG_DIALECT = postgresql.dialect()


class RelationKind(StrEnum):
    TABLE = "table"
    VIEW = "view"


class ColumnDescription(BaseModel):
    type: str
    comment: str | None = None
    pk: bool = False
    required: bool = False


class CheckConstraintDescription(BaseModel):
    name: str
    expression: str


class RelationDefinition(BaseModel):
    name: str
    kind: RelationKind
    description: str | None = None
    columns: dict[str, ColumnDescription]
    check_constraints: list[CheckConstraintDescription] = []


def describe_table(table_name: str) -> RelationDefinition | None:
    """Describe a single table or view by name."""
    table = Base.metadata.tables.get(table_name)
    if table is None:
        return None

    is_view = table.info.get("is_view", False)
    columns = {
        col.name: ColumnDescription(
            type=col.type.compile(dialect=_PG_DIALECT),
            comment=col.comment,
            pk=col.primary_key,
            required=not col.nullable and not col.primary_key,
        )
        for col in table.columns
    }

    check_constraints = [
        CheckConstraintDescription(
            name=c.name or "",
            expression=str(c.sqltext.compile(dialect=_PG_DIALECT, compile_kwargs={"literal_binds": True})),
        )
        for c in table.constraints
        if isinstance(c, CheckConstraint) and c.name
    ]

    return RelationDefinition(
        name=table_name,
        kind=RelationKind.VIEW if is_view else RelationKind.TABLE,
        description=table.comment,
        columns=columns,
        check_constraints=check_constraints,
    )


def describe_all() -> list[RelationDefinition]:
    """Describe all ORM-mapped tables and views."""
    result: list[RelationDefinition] = []
    for name in sorted(Base.metadata.tables):
        desc = describe_table(name)
        if desc is not None:
            result.append(desc)
    return result
