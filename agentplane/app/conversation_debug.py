"""Typed, paginated evidence reads; raw frames are fetched only on explicit expansion."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue


class ConversationScopeChangedError(ValueError):
    """A reference names a source or projection epoch no longer materialized."""


class ConversationEvidenceNotFoundError(LookupError):
    """The selected entity or observation has no association in this conversation."""


class ConversationPayloadIncompleteError(LookupError):
    """Stored chunks do not assemble to the byte count their manifest revision records."""


class EvidenceObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_cursor: str
    has_native: bool


class EvidencePage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observations: list[EvidenceObservation]
    next_after_cursor: str | None


class NativeFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_sequence: str
    availability: Literal["present", "unavailable"]
    entry: dict[str, JsonValue] | None


class NativeFramePage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frames: list[NativeFrame]
    next_after_sequence: str | None


class ArchivedObservation(BaseModel):
    """Identity and kind only; the raw entry is fetched per observation on expansion."""

    model_config = ConfigDict(extra="forbid")

    cursor: str
    source_id: str
    source_sequence: str
    kind: str


class ObservationPage(BaseModel):
    """One chronological archive window, including observations without projected items."""

    model_config = ConfigDict(extra="forbid")

    observations: list[ArchivedObservation]
    next_before_cursor: str | None
    next_after_cursor: str | None


class ArchivedObservationEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cursor: str
    entry: dict[str, JsonValue]


class ConversationPayloadPresent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    availability: Literal["present"]
    body: str


class ConversationPayloadUnavailable(BaseModel):
    """The manifest records the revision but holds no content for it."""

    model_config = ConfigDict(extra="forbid")

    availability: Literal["unavailable"]


ConversationPayloadBody = ConversationPayloadPresent | ConversationPayloadUnavailable
