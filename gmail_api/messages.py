"""Pydantic models mirroring Gmail's message and draft REST resources.

Field names and shapes follow the Gmail API (camelCase on the wire, snake_case in
Python via `to_camel` aliases + `populate_by_name`), so an API JSON response validates
directly with `Model.model_validate(response)` and serializes back unchanged. See:
https://developers.google.com/gmail/api/reference/rest/v1/users.messages
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class MessagePartHeader(BaseModel):
    name: str
    value: str


class MessagePartBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    attachment_id: str | None = None
    size: int | None = None
    data: str | None = Field(default=None, description="base64url-encoded bytes, exactly as Gmail returns them.")


class MessagePart(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    part_id: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    headers: list[MessagePartHeader] | None = None
    body: MessagePartBody | None = None
    parts: list[MessagePart] | None = None


class Message(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    thread_id: str | None = None
    label_ids: list[str] | None = None
    snippet: str | None = None
    history_id: str | None = None
    internal_date: str | None = None
    payload: MessagePart | None = None
    size_estimate: int | None = None
    raw: str | None = Field(default=None, description="Full RFC 2822 message, base64url — present only for format=raw.")


class Draft(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    message: Message | None = None
