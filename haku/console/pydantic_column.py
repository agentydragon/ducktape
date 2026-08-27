"""SQLAlchemy storage for Pydantic-supported values owned by haku-console."""

from typing import Any

from pydantic import TypeAdapter
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator


class PydanticColumn[T](TypeDecorator[T]):
    """JSONB column that validates and serializes a Pydantic-supported type."""

    # A nullable column's absent value must reach Postgres as SQL NULL: a JSON `null` would fail
    # every `IS NULL` check and CHECK constraint while looking identical from Python.
    impl = JSONB(none_as_null=True)
    cache_ok = True

    def __init__(self, pydantic_type: Any):
        super().__init__()
        self._adapter: TypeAdapter[T] = TypeAdapter(pydantic_type)

    def process_bind_param(self, value: T | None, dialect: Any) -> Any:
        del dialect
        if value is None:
            return None
        return self._adapter.dump_python(value, mode="json", by_alias=True, warnings=False)

    def process_result_value(self, value: Any, dialect: Any) -> T | None:
        del dialect
        if value is None:
            return None
        return self._adapter.validate_python(value)
