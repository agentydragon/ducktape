"""Typed models the backend reads from haku-state and serves over JSON.

Mirrors the read-relevant subset of ``haku/base/schema/item.json`` (the write-time
JSON Schema Haku validates against). Both the item's primary ``action`` and its
operator ``actions[]`` are **discriminated unions on ``kind``**, so invalid field
combinations are unrepresentable.

Unknown top-level fields are ignored on parse so a newer item field doesn't break a
not-yet-rebuilt UI; typed values (enum, dates) still validate strictly.

Keep the frontend's ``types.ts`` in sync.
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


# --- primary action (item.action, optional) -----------------------------------


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
    """The UI's read view of a haku-state item — the fields it renders."""

    id: str
    title: str
    body: str
    value: int
    action: PrimaryAction | None = None
    status: ItemStatus
    deadline: datetime | None = None
    actions: list[OperatorAction] = Field(default_factory=list)


# --- API request/response models (the JSON contract for the React SPA) ---------


class Click(BaseModel):
    """A currently-clicked operator action, from the clicks/ overlay."""

    item_id: str
    action_id: str


class DashboardResponse(BaseModel):
    scan_time: str = Field(description="ISO 8601 timestamp of the last haku-state commit (the last scan/update)")
    deployed_commit: str | None = Field(default=None, description="Short SHA the running UI image was built from")
    deployed_commit_url: str | None = Field(default=None, description="Forgejo link to the deployed commit")
    items: list[Item]
    clicks: list[Click]


class FeedbackRequest(BaseModel):
    text: str
    item_id: str | None = Field(
        default=None,
        description="If set, the item this feedback is about (tagged in the intake note); else a global note",
    )


# --- Improvements / friction surface (improvements.yaml) -----------------------
# Haku's self-backlog: capability ideas + the friction it hits during runs. Read-only
# in the UI (no operator actions) — the operator steers it via feedback / intake.


class ImprovementIdea(BaseModel):
    id: str
    title: str
    value: Literal["high", "medium", "low"]
    status: Literal["recommend", "idea", "parked", "blocked", "wired"]
    summary: str
    detail: str = ""  # markdown


class Friction(BaseModel):
    id: str
    title: str
    severity: Literal["high", "medium", "low"]
    status: Literal["open", "workaround", "resolved", "answered"]
    detail: str = ""  # markdown: impact + fix


class ImprovementsBoard(BaseModel):
    updated: str = ""
    ideas: list[ImprovementIdea] = Field(default_factory=list)
    friction: list[Friction] = Field(default_factory=list)
