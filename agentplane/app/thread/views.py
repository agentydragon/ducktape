"""What a thread's readers see: each synchronized entity row, validated by its kind, and the
thread list's summary."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agentplane.app import thread_fold
from agentplane.app.presets import Harness


class EntityKind(StrEnum):
    VIEW_STATE = "view_state"
    ITEM = "item"
    CONFIRMED_INPUT = "confirmed_input"
    LIFECYCLE = "lifecycle"
    COMMAND = "command"


# Kinds positioned in the thread and paged by cursor; view state and command rows sync whole.
SEGMENT_KINDS = (EntityKind.ITEM, EntityKind.CONFIRMED_INPUT, EntityKind.LIFECYCLE)


class ThreadPayloadReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projection_epoch: str
    owner_cursor: str
    owner_id: str
    field: thread_fold.PayloadField
    revision_cursor: str
    generation: str


class ThreadControlsState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied_model: str | None
    active_turn_id: str | None
    harness_state: str | None


class ThreadFeedErrorState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cursor: str | None
    message: str


class ThreadOperationalState(BaseModel):
    """Feed lifecycle state with an independent version, never a fabricated runner cursor."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "ended", "failed"]
    last_verified_cursor: str
    feed_error: ThreadFeedErrorState | None


class ThreadViewState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    controls: ThreadControlsState
    operational: ThreadOperationalState


class ThreadItemState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: int
    tool_name: str
    completion: str | None
    tool_succeeded: bool | None


class ThreadConfirmedInputState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    harness_message_id: str
    origin_command_ids: list[str]


class ThreadLifecycleState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation: str
    event: JsonValue


class ThreadCommandState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str
    outcome: thread_fold.CommandOutcome
    outcome_cursor: str | None
    outcome_reason: str | None


class _ThreadEntityViewFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: UUID
    projection_epoch: str
    entity_id: str
    cursor: int
    revision_cursor: int
    pending: bool
    turn_id: str | None
    text_ref: ThreadPayloadReference | None
    arguments_ref: ThreadPayloadReference | None
    output_ref: ThreadPayloadReference | None
    input_ref: ThreadPayloadReference | None


class ThreadViewStateEntityView(_ThreadEntityViewFields):
    entity_kind: Literal[EntityKind.VIEW_STATE]
    state: ThreadViewState


class ThreadItemEntityView(_ThreadEntityViewFields):
    entity_kind: Literal[EntityKind.ITEM]
    state: ThreadItemState


class ThreadConfirmedInputEntityView(_ThreadEntityViewFields):
    entity_kind: Literal[EntityKind.CONFIRMED_INPUT]
    state: ThreadConfirmedInputState


class ThreadLifecycleEntityView(_ThreadEntityViewFields):
    entity_kind: Literal[EntityKind.LIFECYCLE]
    state: ThreadLifecycleState


class ThreadCommandEntityView(_ThreadEntityViewFields):
    entity_kind: Literal[EntityKind.COMMAND]
    state: ThreadCommandState


# The generated client contract for a synchronized current thread entity row.
ThreadEntityView = Annotated[
    ThreadViewStateEntityView
    | ThreadItemEntityView
    | ThreadConfirmedInputEntityView
    | ThreadLifecycleEntityView
    | ThreadCommandEntityView,
    Field(discriminator="entity_kind"),
]


class ThreadView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    sandbox: str
    session_id: str
    harness: Harness = Field(description="The runner protocol Harness enum member.")
    model: str
    cwd: str
    created_at: datetime
    name: str | None = Field(description="The user-given name; None while the thread is unnamed.")
    archived: bool
    last_cursor: int = Field(description="The highest stored follow cursor; 0 while nothing is stored.")
    last_event_at: datetime | None = None
    harness_state: str = Field(
        description="The protocol's HarnessState enum member, by name: HARNESS_STATE_RUNNING, "
        "HARNESS_STATE_STOPPED, or HARNESS_STATE_UNSPECIFIED while no feed has ever attached to this thread."
    )
