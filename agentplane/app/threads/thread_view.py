"""The thread list's summary of one Thread."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agentplane.runner.harness import Harness


class ThreadView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    sandbox: str
    harness: Harness | None = Field(
        description="The runner protocol Harness enum member, from the last attached runner's spec; None "
        "until the Thread's fold summary has a runner attachment."
    )
    model: str | None = Field(
        description="The current model, from the last attached runner's spec; None until an attachment is folded."
    )
    cwd: str | None = Field(
        description="The runner's working directory, from the last attached runner's spec; None until an "
        "attachment is folded."
    )
    created_at: datetime
    name: str | None = Field(description="The user-given name; None while the thread is unnamed.")
    archived: bool
    last_cursor: int = Field(description="The highest stored Event cursor; 0 while nothing is stored.")
    last_event_at: datetime | None = None
    last_model_activity_at: datetime | None = Field(
        default=None,
        description="Inferred from this Thread's model-originated events; not an observed provider request or cache hit.",
    )
    idle_since: datetime | None = Field(
        default=None,
        description="When the harness last became idle, running with no turn in progress; None while a turn runs, "
        "while the harness is stopped, or before the fold has seen the event that sets it.",
    )
    harness_state: str = Field(
        description="The protocol's HarnessState enum member, by name: HARNESS_STATE_RUNNING, "
        "HARNESS_STATE_STOPPED, or HARNESS_STATE_UNSPECIFIED while no feed has ever attached to this thread."
    )
    setup_state: str | None = Field(default=None, description="One-shot Thread setup state, once attached.")
    launch_outcome: str | None = Field(
        default=None,
        description="The protocol's LaunchOutcome enum member, by name: whether the harness's first launch is still "
        "pending, started or failed; None before attachment.",
    )
    active_turn_id: str | None = Field(
        default=None, description="The runner's currently active turn, or None when it is idle."
    )
    reasoning_effort: str | None = Field(
        default=None, description="Last attached runner reasoning effort; None before attachment."
    )
    feed_status: Literal["active", "ended", "failed"] | None = Field(
        default=None, description="Runner feed lifecycle; None before the first attachment."
    )
    last_turn_status: str | None = Field(
        default=None,
        description="The protocol's TurnStatus enum member, by name, of the thread's most recently completed turn: "
        "TURN_STATUS_COMPLETED, TURN_STATUS_INTERRUPTED, TURN_STATUS_FAILED, TURN_STATUS_PROCESS_LOST, or "
        "TURN_STATUS_UNSPECIFIED when the turn ended without a status; None while no turn has completed.",
    )
