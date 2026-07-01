"""Tool-facing models for the gmail_labeling MCP server."""

from pydantic import BaseModel, Field


class Label(BaseModel):
    """A Gmail label in the managed namespace."""

    name: str = Field(description="Full label display name, e.g. 'haku/triaged'.")
    id: str = Field(description="Gmail label ID.")


class ModifyLabelsResult(BaseModel):
    """Result of a batched label add/remove across one or more threads."""

    added: list[Label] = Field(description="Labels added to every thread in the batch (created if they were new).")
    removed: list[Label] = Field(description="Labels removed from every thread in the batch.")
