"""Claude Code stream-json frames, as observed from build 2.1.252 under
`--input-format stream-json --output-format stream-json --include-partial-messages
--replay-user-messages --permission-prompt-tool stdio`.

Inbound frames are what the harness writes to stdout; outbound frames are what a driver writes to
its stdin. Only the fields a consumer reads are modeled; the rest of a frame is ignored on the way
in, and `Native` evidence keeps the exact line. A frame, stream event, delta, or control request of
a kind not listed here decodes to its `Unknown*` variant.
"""

from __future__ import annotations

import enum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, TypeAdapter

from x.agentplane.native.claude.blocks import Block
from x.agentplane.native.omit_none import OmitNone
from x.agentplane.native.tagged import UNKNOWN, tag_or_unknown


class CommandState(enum.StrEnum):
    QUEUED = "queued"
    STARTED = "started"
    COMPLETED = "completed"
    # A queued input dropped by an interrupt or a failed turn.
    CANCELLED = "cancelled"


# Control requests the harness sends and expects a control response to.


class CanUseTool(BaseModel):
    subtype: Literal["can_use_tool"]
    tool_name: str
    input: dict[str, Any]
    tool_use_id: str | None = None


class HookCallback(BaseModel):
    """A registered hook firing; the CLI waits for the control response as the hook's output."""

    subtype: Literal["hook_callback"]
    callback_id: str
    input: dict[str, Any]
    tool_use_id: str | None = None


class UnknownControlRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    subtype: str


ControlRequestBody = Annotated[
    Annotated[CanUseTool, Tag("can_use_tool")]
    | Annotated[HookCallback, Tag("hook_callback")]
    | Annotated[UnknownControlRequest, Tag(UNKNOWN)],
    Discriminator(tag_or_unknown("subtype", frozenset({"can_use_tool", "hook_callback"}))),
]


class ControlRequestFrame(BaseModel):
    type: Literal["control_request"]
    request_id: str
    request: ControlRequestBody


class ControlResponseBody(BaseModel):
    subtype: Literal["success", "error"]
    request_id: str
    response: dict[str, Any] | None = None
    error: str | None = None


class ControlResponseFrame(BaseModel):
    type: Literal["control_response"]
    response: ControlResponseBody


class CommandLifecycleFrame(BaseModel):
    """Emitted per user frame uuid under `--replay-user-messages`; `queued` is command-queue
    admission, not transcript admission or durable persistence."""

    type: Literal["command_lifecycle"]
    command_uuid: str
    # A state outside CommandState stays a string: the harness is the writer and may add states.
    # Left-to-right, since smart mode would take the plain string for known states too.
    state: CommandState | str = Field(union_mode="left_to_right")
    uuid: str
    session_id: str


class SystemFrame(BaseModel):
    """`init`, `status`, `api_retry`, and other harness notices; subtype-specific fields stay in
    `model_extra`."""

    model_config = ConfigDict(extra="allow")

    type: Literal["system"]
    subtype: str
    session_id: str | None = None


# Streamed model output under `--include-partial-messages`.


class TextDelta(BaseModel):
    type: Literal["text_delta"]
    text: str


class ThinkingDelta(BaseModel):
    type: Literal["thinking_delta"]
    thinking: str


class InputJsonDelta(BaseModel):
    type: Literal["input_json_delta"]
    partial_json: str


class SignatureDelta(BaseModel):
    type: Literal["signature_delta"]
    signature: str


class UnknownDelta(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str


Delta = Annotated[
    Annotated[TextDelta, Tag("text_delta")]
    | Annotated[ThinkingDelta, Tag("thinking_delta")]
    | Annotated[InputJsonDelta, Tag("input_json_delta")]
    | Annotated[SignatureDelta, Tag("signature_delta")]
    | Annotated[UnknownDelta, Tag(UNKNOWN)],
    Discriminator(
        tag_or_unknown("type", frozenset({"text_delta", "thinking_delta", "input_json_delta", "signature_delta"}))
    ),
]


class StreamedMessage(BaseModel):
    id: str


class MessageStart(BaseModel):
    type: Literal["message_start"]
    message: StreamedMessage


class ContentBlockStart(BaseModel):
    type: Literal["content_block_start"]
    index: int
    content_block: Block


class ContentBlockDelta(BaseModel):
    type: Literal["content_block_delta"]
    index: int
    delta: Delta


class ContentBlockStop(BaseModel):
    type: Literal["content_block_stop"]
    index: int


class UnknownStreamEvent(BaseModel):
    """`message_delta`, `message_stop`, and anything newer."""

    model_config = ConfigDict(extra="allow")

    type: str


StreamEvent = Annotated[
    Annotated[MessageStart, Tag("message_start")]
    | Annotated[ContentBlockStart, Tag("content_block_start")]
    | Annotated[ContentBlockDelta, Tag("content_block_delta")]
    | Annotated[ContentBlockStop, Tag("content_block_stop")]
    | Annotated[UnknownStreamEvent, Tag(UNKNOWN)],
    Discriminator(
        tag_or_unknown(
            "type", frozenset({"message_start", "content_block_start", "content_block_delta", "content_block_stop"})
        )
    ),
]


class StreamEventFrame(BaseModel):
    type: Literal["stream_event"]
    event: StreamEvent
    session_id: str
    uuid: str


class AssistantMessage(BaseModel):
    id: str
    content: list[Block]


class AssistantFrame(BaseModel):
    """One completed content block per frame while streaming; every block of the message after a
    non-streaming retry."""

    type: Literal["assistant"]
    message: AssistantMessage
    session_id: str
    uuid: str


class UserMessage(BaseModel):
    role: Literal["user"]
    content: str | list[Block]


class UserFrame(BaseModel):
    """The harness's own user turns: the replay echo of an input, and tool results."""

    type: Literal["user"]
    message: UserMessage
    uuid: str
    session_id: str | None = None
    is_replay: bool = Field(default=False, alias="isReplay")
    # Structured for a successful tool, a plain message for a failed one.
    tool_use_result: dict[str, Any] | str | None = None


class ResultFrame(BaseModel):
    type: Literal["result"]
    subtype: str
    is_error: bool
    result: str | None = None
    terminal_reason: str | None = None
    stop_reason: str | None = None
    session_id: str
    user_message_uuid: str | None = None
    num_turns: int


class UnknownFrame(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str


ClaudeFrame = Annotated[
    Annotated[ControlRequestFrame, Tag("control_request")]
    | Annotated[ControlResponseFrame, Tag("control_response")]
    | Annotated[CommandLifecycleFrame, Tag("command_lifecycle")]
    | Annotated[SystemFrame, Tag("system")]
    | Annotated[StreamEventFrame, Tag("stream_event")]
    | Annotated[AssistantFrame, Tag("assistant")]
    | Annotated[UserFrame, Tag("user")]
    | Annotated[ResultFrame, Tag("result")]
    | Annotated[UnknownFrame, Tag(UNKNOWN)],
    Discriminator(
        tag_or_unknown(
            "type",
            frozenset(
                {
                    "control_request",
                    "control_response",
                    "command_lifecycle",
                    "system",
                    "stream_event",
                    "assistant",
                    "user",
                    "result",
                }
            ),
        )
    ),
]

_frames: TypeAdapter[ClaudeFrame] = TypeAdapter(ClaudeFrame)


def parse_frame(frame: dict[str, Any]) -> ClaudeFrame:
    return _frames.validate_python(frame)


# Outbound frames.


class HookMatcher(BaseModel):
    """One registration entry for a hook event, matching every tool. A firing arrives as a
    `hook_callback` carrying one of these ids; without a matcher the CLI's schema wants none sent."""

    model_config = ConfigDict(validate_by_name=True, serialize_by_alias=True)

    hook_callback_ids: list[str] = Field(alias="hookCallbackIds")


class InitializeBody(OmitNone):
    """The options an `initialize` carries, none of them settable anywhere else in a session.

    `hooks` registers the callbacks answering each hook event. `append_system_prompt` is added to
    the harness's own system prompt for every turn, leaving its coding-agent policy in place, unlike
    the `systemPrompt` slot beside it, which replaces the prompt outright.
    """

    model_config = ConfigDict(validate_by_name=True, serialize_by_alias=True)

    subtype: Literal["initialize"] = "initialize"
    hooks: dict[str, list[HookMatcher]] | None = None
    append_system_prompt: str | None = Field(default=None, alias="appendSystemPrompt")


class InitializeRequest(BaseModel):
    type: Literal["control_request"] = "control_request"
    request_id: str = Field(default_factory=lambda: f"capture-{uuid4().hex}")
    request: InitializeBody = Field(default_factory=InitializeBody)


class InterruptBody(BaseModel):
    subtype: Literal["interrupt"] = "interrupt"
    reason: str
    cancel_queued: bool


class InterruptRequest(BaseModel):
    type: Literal["control_request"] = "control_request"
    request_id: str = Field(default_factory=lambda: f"capture-{uuid4().hex}")
    request: InterruptBody


class UserInput(BaseModel):
    type: Literal["user"] = "user"
    message: UserMessage
    parent_tool_use_id: None = None
    uuid: str = Field(default_factory=lambda: str(uuid4()))


class ControlResponse(BaseModel):
    type: Literal["control_response"] = "control_response"
    response: ControlResponseBody


Outbound = InitializeRequest | InterruptRequest | UserInput | ControlResponse
