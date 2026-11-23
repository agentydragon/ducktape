"""Pydantic models for Gmail API message and filter resources.

Label models and helpers live in `gmail_api.labels`.
See: https://developers.google.com/gmail/api/reference/rest
"""

from pydantic import BaseModel, ConfigDict, Field

from gmail_api.filters import FilterCriteria as ApiFilterCriteria
from gmail_api.messages import MessagePartHeader


class GmailMessageMinimal(BaseModel):
    """Gmail message from API response (format=minimal or format=raw).

    This is the minimal metadata returned by format=minimal (no headers).
    For headers, use GmailMessageWithHeaders instead.
    """

    id: str
    thread_id: str | None = Field(default=None, alias="threadId")
    label_ids: list[str] = Field(default_factory=list, alias="labelIds")
    internal_date: str = Field(alias="internalDate")  # milliseconds since epoch as string
    snippet: str | None = None

    model_config = {"populate_by_name": True}


class GmailMessagePayload(BaseModel):
    """Payload section of Gmail message (format=metadata)."""

    headers: list[MessagePartHeader] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class GmailMessageWithHeaders(GmailMessageMinimal):
    """Gmail message with headers from API response (format=metadata).

    Use this when you need Subject, From, Date, etc. headers but not the body.
    """

    internal_date: str = Field(alias="internalDate")  # milliseconds since epoch as string
    snippet: str | None = None
    payload: GmailMessagePayload = Field(default_factory=GmailMessagePayload)

    model_config = {"populate_by_name": True}

    def get_header(self, name: str) -> str | None:
        """Get a header value by name (case-sensitive)."""
        for h in self.payload.headers:
            if h.name == name:
                return h.value
        return None

    @property
    def subject(self) -> str:
        return self.get_header("Subject") or ""

    @property
    def sender(self) -> str:
        return self.get_header("From") or ""

    @property
    def date_header(self) -> str | None:
        return self.get_header("Date")


class FilterCriteria(ApiFilterCriteria):
    """Reject unknown criteria so local replay cannot match more messages than the Gmail filter."""

    model_config = ConfigDict(extra="forbid")


class FilterAction(BaseModel):
    """Gmail filter actions to perform on matching messages."""

    add_label_ids: list[str] = Field(default_factory=list, alias="addLabelIds")
    remove_label_ids: list[str] = Field(default_factory=list, alias="removeLabelIds")
    forward: str | None = None

    model_config = {"populate_by_name": True}


class GmailFilter(BaseModel):
    """Gmail filter resource."""

    id: str | None = None
    criteria: FilterCriteria = Field(default_factory=FilterCriteria)
    action: FilterAction = Field(default_factory=FilterAction)


class CreateFilterRequest(BaseModel):
    """Request body for creating a filter."""

    criteria: FilterCriteria
    action: FilterAction
