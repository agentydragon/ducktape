"""Tool-facing models for the gmail_labeling MCP server."""

from pydantic import BaseModel, Field


class Label(BaseModel):
    """A Gmail label in the managed namespace."""

    name: str = Field(description="Full label display name, e.g. 'haku/triaged'.")
    id: str = Field(description="Gmail label ID.")
