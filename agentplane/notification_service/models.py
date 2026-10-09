"""Agent-facing HTTP models. Source content remains provider-defined."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agentplane.notification_service.sources.github_models import GitHubEvent, GitHubSource


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True, hide_input_in_errors=True)


class SourceView(Model):
    subscription_schema: dict[str, JsonValue] = Field(
        description="Provider-defined JSON Schema for subscription creation."
    )
    content: str = Field(description="Description of the retained source payload.")


class DestinationRef(Model):
    namespace: str = Field(min_length=1, max_length=63)
    name: str = Field(min_length=1, max_length=63)
    uid: str = Field(min_length=1, max_length=128)


class ActionsSource(Model):
    provider: Literal["actions"]
    request_id: UUID
    after_sequence: int = Field(default=0, ge=0, le=2**31 - 1)


# Only implemented providers belong in these tagged unions.
type Source = Annotated[ActionsSource | GitHubSource, Field(discriminator="provider")]


class ActionsEvent(Model):
    provider: Literal["actions"]
    request_id: UUID
    sequence: int = Field(ge=1)


class SourceFailureKind(StrEnum):
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    ACCESS_DENIED = "access_denied"
    SOURCE_CHANGED = "source_changed"
    PROCESSING_ERROR = "processing_error"


type EventIdentity = Annotated[ActionsEvent | GitHubEvent, Field(discriminator="provider")]


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


class GitHubRefreshStatus(Model):
    last_success_at: datetime | None
    error_kind: SourceFailureKind | None
    error: str | None
    error_since: datetime | None
    error_observed_at: datetime | None
    retry_at: datetime | None
    refreshing_until: datetime | None


class GitHubAccessStatus(GitHubRefreshStatus):
    app_id: int
    installation_id: int
    repository_id: int
    checked_at: datetime | None
    valid_until: datetime | None
    currently_valid: bool


class GitHubSubjectStatus(GitHubRefreshStatus):
    repository_id: int
    kind: str
    subject_key: str


class GitHubStatus(Model):
    access: list[GitHubAccessStatus]
    subject: GitHubSubjectStatus


class SubscriptionView(Model):
    github: GitHubStatus | None
    id: UUID
    inbox_id: UUID
    source: Source
    idempotency_key: str
    version: int
    cancelled: bool
    expires_at: datetime
    last_success_at: datetime | None
    error_kind: SourceFailureKind | None
    error_since: datetime | None
    error_observed_at: datetime | None
    error: str | None
    retry_at: datetime | None


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


class SubscriptionStatus(Model):
    github: GitHubStatus | None
    id: UUID
    source: Source
    cancelled: bool
    expires_at: datetime
    last_success_at: datetime | None
    error_kind: SourceFailureKind | None
    error_since: datetime | None
    error_observed_at: datetime | None
    error: str | None
    next_source_check_at: datetime | None


class InboxEntrySummary(Model):
    cursor: int
    created_at: datetime
    provider: str
    summary: str


class InboxStatus(Model):
    inbox: InboxView
    notice: NoticeView | None
    subscriptions: list[SubscriptionStatus]
    unannounced_count: int
    pending_acknowledgement_count: int
    pending_entries: list[InboxEntrySummary]
    pending_entries_more: bool
    notice_due_at: datetime | None
    quiet_until: datetime | None
    max_wait_at: datetime | None
    notice_wait_reason: str | None
    next_work_at: datetime | None


class SandboxNotificationStatus(Model):
    observed_at: datetime
    inboxes: list[InboxStatus]
