from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class EmailSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["email"] = "email"
    recipient: str


class ThreadSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["thread"] = "thread"
    max_results: int | None = None


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search: Annotated[EmailSearch | ThreadSearch, Field(discriminator="kind")]
    note: str | None = None
