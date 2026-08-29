"""Whose voice a prompt is: the origin arms a stored prompt carries."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PromptOriginKind(StrEnum):
    """Which arm of `PromptOrigin` a prompt carries.

    Its own vocabulary, overlapping `ChannelSurface` only at `MATRIX`: this discriminates a value
    stored inside a `prompt_enqueued` body, `ChannelSurface` names which channel holds a
    conversation's copy, and one enum for both would make a change to either meaning rewrite the
    other's stored strings. `SPA` (the operator typing into the console) and `HARNESS` (the harness
    resuming its own session) have no surface at all — neither is a channel anything can attach to.
    """

    SPA = "spa"
    MATRIX = "matrix"
    HARNESS = "harness"


class SpaOrigin(BaseModel):
    """The operator typed this into the console.

    **No address, and that is not an inconsistency with the room's arm.** An address exists so a
    channel can tell its own copy of a prompt from a sibling attachment's; a browser tab holds no
    copy to confuse, because it renders the record rather than keeping one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[PromptOriginKind.SPA] = PromptOriginKind.SPA


class MatrixOrigin(BaseModel):
    """The operator said this in a Matrix room, as one or more events folded into one prompt.

    **Both strings are opaque to everything but the Matrix channel.** Only the channel that minted
    an origin may look inside one; **everything else compares, it never interprets**. That is what
    lets the conversation layer hold a channel's address without learning its vocabulary
    (<../docs/conversation_layers.md>).

    **`address` is why this is not just a ref.** One bot serves many rooms, so a bare event id
    cannot tell a sibling room's copy from this room's — and telling them apart is the whole job of
    the reader this exists for.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[PromptOriginKind.MATRIX] = PromptOriginKind.MATRIX
    address: str = Field(description="Which room. Never parsed outside the Matrix channel.")
    refs: tuple[str, ...] = Field(description="The events folded into this prompt, oldest first.")


class HarnessOrigin(BaseModel):
    """The harness resumed the session itself — nobody typed this.

    Claude Code wakes its own session to observe work it left running: a background command's
    completion notification, a `ScheduleWakeup` firing. The exchange that follows has no operator
    behind it, and a transcript that rendered its opening as the operator speaking would put words
    in their mouth. What woke the harness is the prompt item's own text; the origin only has to say
    whose voice it is.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[PromptOriginKind.HARNESS] = PromptOriginKind.HARNESS


type PromptOrigin = SpaOrigin | MatrixOrigin | HarnessOrigin

# The console's own surface, as one value rather than one per call: `SpaOrigin` carries nothing, so
# every instance is the same statement and a shared frozen one says so.
SPA_ORIGIN = SpaOrigin()
HARNESS_ORIGIN = HarnessOrigin()
