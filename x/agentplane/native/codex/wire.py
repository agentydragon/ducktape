"""Codex app-server JSON-RPC frames, per `codex-rs/app-server-protocol/src/protocol/v2` at
rust-v0.152.0 (serde `camelCase`).

Inbound frames are what the app-server writes to stdout: responses to our requests, requests of its
own that block a turn until answered, and notifications. Only the notifications and items a consumer
reads are modeled; a notification method or item type not listed here decodes to its `Unknown*`
variant, and `Native` evidence keeps the exact line.
"""

from __future__ import annotations

import enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, TypeAdapter
from pydantic.alias_generators import to_camel

from x.agentplane.native.omit_none import OmitNone
from x.agentplane.native.tagged import UNKNOWN, tag_or_unknown

RequestId = str | int


class Wire(BaseModel):
    """Field names stay Pythonic; the wire is camelCase in both directions."""

    model_config = ConfigDict(
        alias_generator=to_camel, validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )


class RpcError(Wire):
    code: int
    message: str
    data: Any = None


class ServerRequest(Wire):
    """A request from the app-server (approval, user input, elicitation); the turn blocks on it."""

    id: RequestId
    method: str
    params: dict[str, Any] | None = None


class Response(Wire):
    id: RequestId
    result: dict[str, Any] | None = None
    error: RpcError | None = None


# Results of the requests a driver sends.


class ThreadSummary(Wire):
    id: str


class ThreadResult(Wire):
    thread: ThreadSummary


class TurnStatus(enum.StrEnum):
    IN_PROGRESS = "inProgress"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class TurnError(Wire):
    message: str


class Turn(Wire):
    id: str
    # A status outside TurnStatus stays a string: the harness is the writer and may add statuses.
    # Left-to-right, since smart mode would take the plain string for known statuses too.
    status: TurnStatus | str = Field(union_mode="left_to_right")
    error: TurnError | None = None


class TurnResult(Wire):
    turn: Turn


# Thread items.


class UserInputText(Wire):
    type: Literal["text"]
    text: str


class UnknownUserInput(Wire):
    model_config = ConfigDict(extra="allow")

    type: str


UserInput = Annotated[
    Annotated[UserInputText, Tag("text")] | Annotated[UnknownUserInput, Tag(UNKNOWN)],
    Discriminator(tag_or_unknown("type", frozenset({"text"}))),
]


class UserMessageItem(Wire):
    type: Literal["userMessage"]
    id: str
    content: list[UserInput]


class AgentMessageItem(Wire):
    type: Literal["agentMessage"]
    id: str
    text: str


class ReasoningItem(Wire):
    type: Literal["reasoning"]
    id: str
    summary: list[str] = []
    content: list[str] = []


class CommandExecutionStatus(enum.StrEnum):
    IN_PROGRESS = "inProgress"
    COMPLETED = "completed"
    FAILED = "failed"
    DECLINED = "declined"


class CommandExecutionItem(Wire):
    type: Literal["commandExecution"]
    id: str
    command: str
    cwd: str
    status: CommandExecutionStatus | str = Field(union_mode="left_to_right")
    process_id: str | None = None
    aggregated_output: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None


class UnknownItem(Wire):
    """`fileChange`, `mcpToolCall`, `webSearch`, and the rest of the catalog."""

    model_config = ConfigDict(extra="allow")

    type: str
    id: str


Item = Annotated[
    Annotated[UserMessageItem, Tag("userMessage")]
    | Annotated[AgentMessageItem, Tag("agentMessage")]
    | Annotated[ReasoningItem, Tag("reasoning")]
    | Annotated[CommandExecutionItem, Tag("commandExecution")]
    | Annotated[UnknownItem, Tag(UNKNOWN)],
    Discriminator(tag_or_unknown("type", frozenset({"userMessage", "agentMessage", "reasoning", "commandExecution"}))),
]


# Notifications.


class TurnParams(Wire):
    thread_id: str
    turn: Turn


class ItemParams(Wire):
    thread_id: str
    turn_id: str
    item: Item


class ItemDeltaParams(Wire):
    thread_id: str
    turn_id: str
    item_id: str
    delta: str


class ErrorParams(Wire):
    error: TurnError
    will_retry: bool
    thread_id: str
    turn_id: str


class TurnStarted(Wire):
    method: Literal["turn/started"]
    params: TurnParams


class TurnCompleted(Wire):
    method: Literal["turn/completed"]
    params: TurnParams


class ItemStarted(Wire):
    method: Literal["item/started"]
    params: ItemParams


class ItemCompleted(Wire):
    method: Literal["item/completed"]
    params: ItemParams


class AgentMessageDelta(Wire):
    method: Literal["item/agentMessage/delta"]
    params: ItemDeltaParams


class ReasoningSummaryTextDelta(Wire):
    method: Literal["item/reasoning/summaryTextDelta"]
    params: ItemDeltaParams


class CommandExecutionOutputDelta(Wire):
    method: Literal["item/commandExecution/outputDelta"]
    params: ItemDeltaParams


class ErrorNotification(Wire):
    """A model request failure; `will_retry` says whether the harness tries again on its own."""

    method: Literal["error"]
    params: ErrorParams


class UnknownNotification(Wire):
    method: str
    params: Any = None


Notification = Annotated[
    Annotated[TurnStarted, Tag("turn/started")]
    | Annotated[TurnCompleted, Tag("turn/completed")]
    | Annotated[ItemStarted, Tag("item/started")]
    | Annotated[ItemCompleted, Tag("item/completed")]
    | Annotated[AgentMessageDelta, Tag("item/agentMessage/delta")]
    | Annotated[ReasoningSummaryTextDelta, Tag("item/reasoning/summaryTextDelta")]
    | Annotated[CommandExecutionOutputDelta, Tag("item/commandExecution/outputDelta")]
    | Annotated[ErrorNotification, Tag("error")]
    | Annotated[UnknownNotification, Tag(UNKNOWN)],
    Discriminator(
        tag_or_unknown(
            "method",
            frozenset(
                {
                    "turn/started",
                    "turn/completed",
                    "item/started",
                    "item/completed",
                    "item/agentMessage/delta",
                    "item/reasoning/summaryTextDelta",
                    "item/commandExecution/outputDelta",
                    "error",
                }
            ),
        )
    ),
]

CodexFrame = ServerRequest | Response | Notification

_notifications: TypeAdapter[Notification] = TypeAdapter(Notification)


def parse_frame(frame: dict[str, Any]) -> CodexFrame:
    """Classify by JSON-RPC shape: a method with an id is a request, an id alone a response, a
    method alone a notification."""
    if "method" in frame:
        if "id" in frame:
            return ServerRequest.model_validate(frame)
        return _notifications.validate_python(frame)
    if "id" in frame:
        return Response.model_validate(frame)
    raise ValueError(f"not a JSON-RPC frame: {sorted(frame)}")


# Requests a driver sends, and the one reply it gives to a server request.


class ClientInfo(Wire):
    name: str
    version: str


class InitializeParams(Wire):
    client_info: ClientInfo
    capabilities: None = None


class InitializeRequest(Wire):
    method: Literal["initialize"] = "initialize"
    id: RequestId
    params: InitializeParams


class InitializedNotification(Wire):
    method: Literal["initialized"] = "initialized"


class ThreadStartParams(Wire, OmitNone):
    """`base_instructions` replaces the app-server's coding-agent policy; `developer_instructions`
    is carried beside it, and the thread keeps it for every turn the thread ever runs."""

    cwd: str
    approval_policy: str
    sandbox: str
    ephemeral: bool
    model: str
    base_instructions: str
    developer_instructions: str | None = None
    config: dict[str, Any]


class ThreadStartRequest(Wire):
    method: Literal["thread/start"] = "thread/start"
    id: RequestId
    params: ThreadStartParams


class ThreadResumeParams(Wire, OmitNone):
    thread_id: str
    base_instructions: str | None = None
    developer_instructions: str | None = None


class ThreadResumeRequest(Wire):
    method: Literal["thread/resume"] = "thread/resume"
    id: RequestId
    params: ThreadResumeParams


class TextInput(BaseModel):
    """Not a `Wire` model: serde renames the `UserInput` variant, not its fields, so the wire is
    snake_case here."""

    type: Literal["text"] = "text"
    text: str
    text_elements: list[Any] = []


class TurnStartParams(Wire):
    thread_id: str
    input: list[TextInput]


class TurnStartRequest(Wire):
    method: Literal["turn/start"] = "turn/start"
    id: RequestId
    params: TurnStartParams


class TurnSteerParams(Wire):
    thread_id: str
    # The steer is rejected unless this names the currently active turn.
    expected_turn_id: str
    input: list[TextInput]


class TurnSteerRequest(Wire):
    method: Literal["turn/steer"] = "turn/steer"
    id: RequestId
    params: TurnSteerParams


class TurnInterruptParams(Wire):
    thread_id: str
    turn_id: str


class TurnInterruptRequest(Wire):
    method: Literal["turn/interrupt"] = "turn/interrupt"
    id: RequestId
    params: TurnInterruptParams


class ErrorResponse(Wire):
    id: RequestId
    error: RpcError


# Outbound frames that carry an id and so get a `Response`; the notification and the error answer
# to a server request do not.
Request = (
    InitializeRequest
    | ThreadStartRequest
    | ThreadResumeRequest
    | TurnStartRequest
    | TurnSteerRequest
    | TurnInterruptRequest
)
