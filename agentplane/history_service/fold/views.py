"""What a thread's readers see: each synchronized entity row, validated by its kind."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class PayloadField(StrEnum):
    TEXT = "text"
    ARGUMENTS = "arguments"
    OUTPUT = "output"
    CONFIRMED_INPUT = "confirmed_input"
    COMMAND_INPUT = "command_input"


class CommandOutcome(StrEnum):
    PENDING = "pending"
    EFFECTED = "effected"
    FAILED = "failed"
    NOOP = "noop"


class EntityKind(StrEnum):
    VIEW_STATE = "view_state"
    ITEM = "item"
    CONFIRMED_INPUT = "confirmed_input"
    LIFECYCLE = "lifecycle"
    COMMAND = "command"


class ThreadPayloadReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projection_epoch: str
    owner_cursor: str
    owner_id: str
    field: PayloadField
    revision_cursor: str
    generation: str
    chunk_count: str = Field(
        description="How many of the generation's chunks this revision spans; a reader renders at most these."
    )


class ThreadControlsState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied_model: str | None
    applied_reasoning_effort: str | None = None
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
    recovery: int | None
    recovery_reason: str


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
    outcome: CommandOutcome
    outcome_cursor: str | None
    outcome_reason: str | None
    requested_value: str | None = None


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
