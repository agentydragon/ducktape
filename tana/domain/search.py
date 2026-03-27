from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from tana.domain.types import NodeId


class SearchKind(StrEnum):
    """Valid search expression kinds."""

    TAG = "tag"
    TYPE = "type"
    TEXT = "text"
    FIELD = "field"
    BOOLEAN = "boolean"


class BooleanOperator(StrEnum):
    AND = "AND"
    OR = "OR"
    NOT = "NOT"


class _SearchBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TagSearch(_SearchBase):
    kind: Literal["tag"] = "tag"
    tag_id: NodeId


class TypeSearch(_SearchBase):
    kind: Literal["type"] = "type"
    type_id: NodeId


class TextSearch(_SearchBase):
    kind: Literal["text"] = "text"
    text: str


class FieldSearch(_SearchBase):
    kind: Literal["field"] = "field"
    field_name: str
    values: list[str]


class BooleanSearch(_SearchBase):
    kind: Literal["boolean"] = "boolean"
    operator: BooleanOperator
    operands: list[SearchExpression]


SearchExpression = Annotated[
    TagSearch | TypeSearch | TextSearch | FieldSearch | BooleanSearch, Field(discriminator="kind")
]


BooleanSearch.model_rebuild()
