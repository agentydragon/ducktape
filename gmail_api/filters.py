"""Pydantic models mirroring Gmail's filter REST resources.

Field names and shapes follow the Gmail API (camelCase on the wire, snake_case in
Python via `to_camel` aliases + `populate_by_name`), so an API JSON response validates
directly with `Model.model_validate(response)` and serializes back unchanged. See:
https://developers.google.com/gmail/api/reference/rest/v1/users.settings.filters
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class SizeComparison(StrEnum):
    """`sizeComparison` for `FilterCriteria.size` — whether the size is under or over."""

    SMALLER = "smaller"
    LARGER = "larger"


class FilterCriteria(BaseModel):
    """Message-matching criteria for a filter (`users.settings.filters` `criteria`)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    from_: str | None = Field(default=None, alias="from", description="The sender.")
    to: str | None = Field(default=None, description="The recipient.")
    subject: str | None = Field(default=None)
    query: str | None = Field(default=None, description="Gmail search query, only return messages matching it.")
    negated_query: str | None = Field(
        default=None, description="Gmail search query, only return messages NOT matching it."
    )
    has_attachment: bool | None = Field(default=None)
    exclude_chats: bool | None = Field(default=None)
    size: int | None = Field(default=None, description="Message size in bytes; paired with `size_comparison`.")
    size_comparison: SizeComparison | None = Field(default=None)


class FilterAction(BaseModel):
    """Action a filter performs on its matched messages (`users.settings.filters` `action`)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    add_label_ids: list[str] | None = Field(default=None)
    remove_label_ids: list[str] | None = Field(default=None)
    forward: str | None = Field(default=None, description="Email address to forward matched messages to.")


class GmailFilter(BaseModel):
    """Gmail filter resource."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    criteria: FilterCriteria = Field(default_factory=FilterCriteria)
    action: FilterAction = Field(default_factory=FilterAction)


class FiltersListResponse(BaseModel):
    """Response body of `users.settings.filters.list`.

    Gotcha: Gmail's key is the singular ``filter`` (i.e. ``{"filter": [...]}``), not
    ``filters`` — mirrored verbatim.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    filter: list[GmailFilter] = Field(default_factory=list)
