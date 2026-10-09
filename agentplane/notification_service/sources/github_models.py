"""GitHub-owned source vocabulary and retained delivery identities."""

from collections.abc import Set
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

type RepositoryName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class PullRequestSubject(Model):
    kind: Literal["pull_request"]
    number: int = Field(gt=0)


class BranchSubject(Model):
    kind: Literal["branch"]
    name: str = Field(min_length=1, max_length=255)


class CommitSubject(Model):
    kind: Literal["commit"]
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")


type Subject = Annotated[PullRequestSubject | BranchSubject | CommitSubject, Field(discriminator="kind")]


class EventName(StrEnum):
    PULL_REQUEST = "pull_request"
    ISSUE_COMMENT = "issue_comment"
    PULL_REQUEST_REVIEW = "pull_request_review"
    PULL_REQUEST_REVIEW_COMMENT = "pull_request_review_comment"
    CHECK_RUN = "check_run"
    CHECK_SUITE = "check_suite"
    STATUS = "status"
    PUSH = "push"
    CREATE = "create"
    DELETE = "delete"
    WORKFLOW_RUN = "workflow_run"


CI_EVENTS = frozenset({EventName.CHECK_RUN, EventName.CHECK_SUITE, EventName.STATUS, EventName.WORKFLOW_RUN})
PR_EVENTS = frozenset(
    {
        EventName.PULL_REQUEST,
        EventName.ISSUE_COMMENT,
        EventName.PULL_REQUEST_REVIEW,
        EventName.PULL_REQUEST_REVIEW_COMMENT,
    }
)
REF_EVENTS = frozenset({EventName.PUSH, EventName.CREATE, EventName.DELETE})


class EventFilter(Model):
    model_config = ConfigDict(frozen=True)

    event: EventName
    actions: Set[Annotated[str, Field(pattern=r"^[a-z_]+$", max_length=64)]] | None = Field(
        default=None, min_length=1, max_length=32
    )

    @field_serializer("actions", when_used="json")
    def ordered_actions(self, actions: Set[str] | None) -> list[str] | None:
        return sorted(actions) if actions is not None else None

    @model_validator(mode="after")
    def action_support(self) -> Self:
        if self.actions is not None and self.event in REF_EVENTS | {EventName.STATUS}:
            raise ValueError("this GitHub event has no action selector")
        return self


class GitHubSource(Model):
    provider: Literal["github"]
    repository: RepositoryName
    subject: Subject
    events: Set[EventFilter] | None = Field(default=None, min_length=1, max_length=32)

    @field_serializer("events", when_used="json")
    def ordered_events(self, events: Set[EventFilter] | None) -> list[EventFilter] | None:
        return sorted(events, key=lambda f: (f.event, tuple(sorted(f.actions or ())))) if events is not None else None

    @model_validator(mode="after")
    def supported_subject_events(self) -> Self:
        allowed = CI_EVENTS
        if isinstance(self.subject, PullRequestSubject):
            allowed |= PR_EVENTS
        elif isinstance(self.subject, BranchSubject):
            allowed |= REF_EVENTS
        if self.events is not None and any(event.event not in allowed for event in self.events):
            raise ValueError("event is not supported for this GitHub subject")
        return self

    @property
    def filters(self) -> Set[EventFilter]:
        if self.events is not None:
            return self.events
        events = {EventFilter(event=EventName.STATUS), EventFilter(event=EventName.CHECK_RUN, actions={"completed"})}
        if isinstance(self.subject, PullRequestSubject):
            events |= {EventFilter(event=event) for event in PR_EVENTS}
        elif isinstance(self.subject, BranchSubject):
            events |= {EventFilter(event=event) for event in REF_EVENTS}
        return events


class GitHubEvent(Model):
    provider: Literal["github"]
    app_id: int = Field(gt=0)
    delivery_id: UUID
    repository_id: int = Field(gt=0)
    event: EventName
    action: str | None = None


class GitHubBinding(Model):
    app_id: int = Field(gt=0)
    installation_id: int = Field(gt=0)
    repository_id: int = Field(gt=0)


def subject_key(subject: Subject) -> str:
    match subject:
        case PullRequestSubject(number=number):
            return str(number)
        case BranchSubject(name=name):
            return name
        case CommitSubject(sha=sha):
            return sha
