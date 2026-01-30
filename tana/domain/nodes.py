from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from tana.domain.types import NodeId


class Props(BaseModel):
    """Metadata associated with a Tana node."""

    created: int | None = None
    name: str | None = None
    doc_type: str | None = Field(alias="_docType", default=None)
    owner_id: NodeId | None = Field(alias="_ownerId", default=None)
    meta_node_id: NodeId | None = Field(alias="_metaNodeId", default=None)
    source_id: NodeId | None = Field(alias="_sourceId", default=None)
    done: int | None = Field(alias="_done", default=None)
    description: str | None = None
    flags: int | None = Field(alias="_flags", default=None)
    image_width: int | None = Field(alias="_imageWidth", default=None)
    image_height: int | None = Field(alias="_imageHeight", default=None)
    published: int | None = Field(alias="_published", default=None)
    view: str | None = Field(alias="_view", default=None)
    edit_mode: bool | None = Field(alias="_editMode", default=None)
    search_context_node: str | None = Field(alias="searchContextNode", default=None)

    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def created_dt(self) -> datetime | None:
        if self.created is None:
            return None
        return datetime.fromtimestamp(self.created / 1_000, tz=UTC)

    @property
    def is_trash(self) -> bool:
        return bool(self.owner_id and self.owner_id.endswith("_TRASH"))


class BaseNode(BaseModel):
    """Common base model for all Tana nodes."""

    id: NodeId
    props: Props
    children: list[NodeId] = Field(default_factory=list)
    modified_ts: list[int] | None = Field(alias="modifiedTs", default=None)
    touch_counts: list[int] | None = Field(alias="touchCounts", default=None)
    association_map: Mapping[NodeId, NodeId] | None = Field(alias="associationMap", default=None)

    model_config = ConfigDict(extra="allow", frozen=True)

    @property
    def name(self) -> str | None:
        return self.props.name

    @property
    def is_trash(self) -> bool:
        return self.props.is_trash


class TupleNode(BaseNode):
    """Node representing a tuple (key/value pairs)."""


class TagDefNode(BaseNode):
    """Node representing a tag definition."""


class VisualNode(BaseNode):
    """Node representing visual content (images).

    Use tana.query.nodes.get_image_url(node) to extract image URLs.
    """


class CodeBlockNode(BaseNode):
    """Node representing a code block."""


class UnknownNode(BaseNode):
    """Fallback node type when _docType is not recognised."""


DOC_CLASS: Mapping[str | None, type[BaseNode]] = {
    "tuple": TupleNode,
    "tagDef": TagDefNode,
    "visual": VisualNode,
    "codeblock": CodeBlockNode,
    None: UnknownNode,
}
