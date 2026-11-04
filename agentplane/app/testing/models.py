"""HTTP contract models used by the app's independent clients and system tests."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agentplane.env.runtime.harness import Harness

SUBSET_ROW_LIMIT = 200


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
    placement_value = spec.get("placement")
    placement = placement_value if isinstance(placement_value, dict) else {}
    native_value = placement.get("environmentHosted")
    native = native_value if isinstance(native_value, dict) else None
    if native is not None:
        harness = native.get("harness")
    elif isinstance(placement.get("centralSamplerV1"), dict):
        harness = Harness.CENTRAL_SAMPLER
    else:
        harness = attachment.get("historicalHarness")
        if harness == "HARNESS_UNSPECIFIED":
            harness = None
    feed_statuses = {"FEED_STATUS_ACTIVE": "active", "FEED_STATUS_ENDED": "ended", "FEED_STATUS_FAILED": "failed"}
    feed_status = feed_statuses.get(str(summary.get("feedStatus"))) if attached is not None else None
    return ThreadView.model_validate(
        {
            "id": metadata["threadId"],
            "sandbox": sandbox.get("name", ""),
            "sandbox_uid": sandbox.get("uid"),
            "harness": harness,
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


class SubsetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    where: str | None = None
    params: dict[str, str] | None = None
    order_by: str | None = None
    limit: int | None = None
