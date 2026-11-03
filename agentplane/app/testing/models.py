"""HTTP contract models used by the app's independent clients and system tests."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agentplane.egress.decisions import Decision
from agentplane.egress.resources import Target
from agentplane.env.runtime.harness import Harness
from agentplane.policy_sets import ConditionStatus
from agentplane.subjects import ServiceAccountRef

SUBSET_ROW_LIMIT = 200


class CredentialView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    placeholder: str
    secret: str | None = None
    key: str | None = None
    targets: list[Target]


class EgressRuleView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["egress"]
    number: int
    hosts: list[str]
    methods: list[str] | None = None
    paths: list[str] | None = None
    credential: CredentialView | None = None
    missing_credential: str | None = None


class OtherRuleView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["other"]
    number: int
    type: str
    fields: dict[str, JsonValue]


RuleView = Annotated[EgressRuleView | OtherRuleView, Field(discriminator="kind")]


class PolicySetView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    generation: int
    rules: list[RuleView]
    refused: str | None = None


class BindingProvenance(StrEnum):
    GIT = "git"
    SANDBOX = "sandbox"
    OPERATOR = "operator"


class ConditionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    status: ConditionStatus
    reason: str
    message: str
    observed_generation: int | None = None


class BindingView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    provenance: BindingProvenance
    generation: int
    subjects: list[ServiceAccountRef]
    expires_at: datetime | None = None
    conditions: list[ConditionView]
    policy_sets: list[PolicySetView]
    missing_policy_sets: list[str]


class ThreadView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    sandbox: str
    sandbox_uid: str | None = None
    harness: Harness | None
    model: str | None
    cwd: str | None
    created_at: datetime
    name: str | None
    archived: bool
    last_cursor: int = Field(description="The Go API serializes this uint64 as a decimal integer or string.")
    last_event_at: datetime | None = None
    last_model_activity_at: datetime | None = None
    idle_since: datetime | None = None
    harness_state: str
    setup_state: str | None = None
    launch_outcome: str | None = None
    active_turn_id: str | None = None
    reasoning_effort: str | None = None
    feed_status: Literal["active", "ended", "failed"] | None = None
    last_turn_status: str | None = None


class ThreadMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: UUID = Field(validation_alias="threadId")
    created_at: datetime = Field(validation_alias="createdAt")
    name: str | None = None
    archived: bool = False

    @property
    def id(self) -> UUID:
        return self.thread_id


def thread_view_from_proto_json(metadata: dict[str, object], summary: dict[str, object]) -> ThreadView:
    """Compose the generated App Thread metadata and History summary for test clients."""
    attached = summary.get("attached")
    attachment = attached if isinstance(attached, dict) else {}
    spec_value = attachment.get("spec")
    spec = spec_value if isinstance(spec_value, dict) else {}
    sandbox_value = summary.get("sandbox")
    sandbox = sandbox_value if isinstance(sandbox_value, dict) else {}
    feed_statuses = {"FEED_STATUS_ACTIVE": "active", "FEED_STATUS_ENDED": "ended", "FEED_STATUS_FAILED": "failed"}
    feed_status = feed_statuses.get(str(summary.get("feedStatus"))) if attached is not None else None
    return ThreadView.model_validate(
        {
            "id": metadata["threadId"],
            "sandbox": sandbox.get("name", ""),
            "sandbox_uid": sandbox.get("uid"),
            "harness": spec.get("harness"),
            "model": spec.get("model"),
            "cwd": spec.get("cwd"),
            "created_at": metadata["createdAt"],
            "name": metadata.get("name"),
            "archived": metadata.get("archived", False),
            "last_cursor": int(str(summary.get("foldedThrough", "0"))),
            "last_event_at": summary.get("lastEventAt"),
            "last_model_activity_at": summary.get("lastModelActivityAt"),
            "idle_since": summary.get("idleSince"),
            "harness_state": attachment.get("harnessState", "HARNESS_STATE_UNSPECIFIED"),
            "setup_state": attachment.get("setupState"),
            "launch_outcome": attachment.get("launchOutcome"),
            "active_turn_id": attachment.get("activeTurnId"),
            "reasoning_effort": spec.get("reasoningEffort"),
            "feed_status": feed_status,
            "last_turn_status": summary.get("lastTurnStatus"),
        }
    )


class DecisionsUnavailable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: str


class DecisionsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    watching: bool
    decisions: list[Decision] | DecisionsUnavailable


class ThreadScopeResponse(BaseModel):
    projection_epoch: str
    through_cursor: str


class SubsetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    where: str | None = None
    params: dict[str, str] | None = None
    order_by: str | None = None
    limit: int | None = None
