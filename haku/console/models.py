"""Typed models for the items the console reads from the haku-state clone.

Mirrors the read-relevant subset of ``haku/base/schema/item.json`` (the write-time
JSON Schema Haku validates against); the console is the read side. Both the item's
primary ``action`` and its operator ``actions[]`` are **discriminated unions on
``kind``**, so invalid field combinations — a ``command`` carrying a handoff's
``prompt``, a ``claude_handoff`` with no ``prompt`` — are unrepresentable.

Unknown top-level fields are ignored on parse (Pydantic's default) so a newer item
field doesn't break a not-yet-rebuilt console; typed values (enum, dates) still
validate strictly.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class ItemStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    REJECTED = "rejected"
    SNOOZED = "snoozed"
    EXPIRED = "expired"


# --- primary action (item.action, required) ----------------------------------


class Suggestion(BaseModel):
    """FYI / 'do this yourself'; no machine payload."""

    kind: Literal["suggestion"] = "suggestion"


class PreparedPrompt(BaseModel):
    kind: Literal["prepared_prompt"] = "prepared_prompt"
    prompt: str = Field(description="Self-contained prompt for an executor agent session")


PrimaryAction = Annotated[Suggestion | PreparedPrompt, Field(discriminator="kind")]


# --- operator action buttons (item.actions[], optional) -----------------------


class CommandAction(BaseModel):
    """A click/un-click toggle; Haku interprets ``intent`` when the click lands."""

    kind: Literal["command"] = "command"
    id: str = Field(description="Stable id within the item (used in the click path)")
    label: str = Field(description="Button text shown to the operator")
    intent: str = Field(description="Self-contained instruction Haku carries out when this is clicked")


class ClaudeHandoffAction(BaseModel):
    """A stateless ``claude.ai/new`` deep-link rendered inline (no click state)."""

    kind: Literal["claude_handoff"] = "claude_handoff"
    id: str = Field(description="Stable id within the item (used in the click path)")
    label: str = Field(description="Button text shown to the operator")
    prompt: str = Field(description="The prepared prompt to hand to Claude via the deep-link")


OperatorAction = Annotated[CommandAction | ClaudeHandoffAction, Field(discriminator="kind")]


class Item(BaseModel):
    """The console's read view of a haku-state item — the fields the dashboard
    renders. (A subset of item.json; Haku-only fields like ``dedup_key`` are
    ignored on parse.)"""

    id: str
    title: str
    body: str
    value: int
    action: PrimaryAction
    status: ItemStatus
    deadline: datetime | None = None
    actions: list[OperatorAction] = Field(default_factory=list)


# --- API request/response models (the JSON contract for the React SPA) ---------


class Click(BaseModel):
    """A currently-clicked operator action, from the clicks/ overlay."""

    item_id: str
    action_id: str


class DashboardResponse(BaseModel):
    scan_time: str
    items: list[Item]
    clicks: list[Click]
    # The launch-routine's claude.ai/code page, surfaced as a "view past runs" deep-link
    # (there's no runs-listing API). None when the launch capability isn't configured.
    # This is a benign public link — the privileged launch action stays on the
    # capability tier (see haku.console.capabilities).
    launch_routine_url: str | None = None


class FeedbackRequest(BaseModel):
    text: str
    item_id: str | None = Field(
        default=None,
        description="If set, the item this feedback is about (tagged in the intake note); else a global note",
    )
