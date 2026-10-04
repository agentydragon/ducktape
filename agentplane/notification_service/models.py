"""Agent-facing HTTP models. Source content remains provider-defined."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True, hide_input_in_errors=True)


class SourceView(Model):
    subscription_schema: dict[str, JsonValue] = Field(
        description="Provider-defined JSON Schema for subscription creation."
    )
    content: str = Field(description="Description of the retained provider payload.")


class DestinationRef(Model):
    namespace: str = Field(min_length=1, max_length=63)
    name: str = Field(min_length=1, max_length=63)
    uid: str = Field(min_length=1, max_length=128)


class ActionsSource(Model):
    provider: Literal["actions"]
    request_id: UUID
    after_sequence: int = Field(default=0, ge=0, le=2**31 - 1)


# Only implemented providers belong in these tagged unions.
type Source = Annotated[ActionsSource, Field(discriminator="provider")]


class ActionsEvent(Model):
    provider: Literal["actions"]
    request_id: UUID
    sequence: int = Field(ge=1)


type EventIdentity = Annotated[ActionsEvent, Field(discriminator="provider")]


class Subscribe(Model):
    destination_ref: DestinationRef
    session_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(
        min_length=1,
        max_length=200,
        description="Inbox-local creation idempotency key. Identical retries return the existing subscription.",
    )
    source: Source
    lifetime_days: int = Field(default=7, ge=1, le=30)


class SubscriptionUpdate(Model):
    version: int = Field(ge=1)
    lifetime_days: int = Field(default=7, ge=1, le=30)


class SubscriptionView(Model):
    id: UUID
    inbox_id: UUID
    source: Source
    idempotency_key: str
    version: int
    cancelled: bool
    expires_at: datetime
    error: str | None


class InboxView(Model):
    id: UUID
    destination_ref: DestinationRef
    session_id: str
    last_cursor: int
    acknowledged: int
    covered: int
    expired_through: int
    retired: bool
    delivery_error: str | None


class EntryView(Model):
    cursor: int
    event: EventIdentity
    payload: dict[str, JsonValue]
    subscriptions: list[UUID]


class NoticeView(Model):
    command_id: UUID
    through_cursor: int
    attempted: bool
    admitted: bool
    confirmed: bool
    error: str | None


class InboxPage(Model):
    inbox: InboxView
    entries: list[EntryView]
    notice: NoticeView | None


class Acknowledge(Model):
    through_cursor: int = Field(ge=0, le=2**63 - 1)
