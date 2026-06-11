from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from mcp import types as mcp_types
from pydantic import BaseModel, ConfigDict, Field

from agent_server.server.protocol import ToolCall

# ---- Display items (normalized, UI-friendly) ----


class UserMessageItem(BaseModel):
    kind: Literal["UserMessage"] = "UserMessage"
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    text: str

    model_config = ConfigDict(extra="forbid")


class AssistantMarkdownItem(BaseModel):
    kind: Literal["AssistantMarkdown"] = "AssistantMarkdown"
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    md: str

    model_config = ConfigDict(extra="forbid")


class EndTurnItem(BaseModel):
    kind: Literal["EndTurn"] = "EndTurn"
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = ConfigDict(extra="forbid")


ApprovalKind = Literal["approve", "deny_continue", "deny_abort"]


# Tool content variants nested under a single ToolItem
class ExecContent(BaseModel):
    content_kind: Literal["Exec"] = "Exec"
    cmd: str | None = None
    args: Any | None = None
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
    is_error: bool | None = None
    model_config = ConfigDict(extra="forbid")


class JsonContent(BaseModel):
    content_kind: Literal["Json"] = "Json"
    args: Any | None = None
    result: mcp_types.CallToolResult | None = None
    is_error: bool | None = None
    model_config = ConfigDict(extra="forbid")


ToolContent = Annotated[ExecContent | JsonContent, Field(discriminator="content_kind")]


class ToolItem(BaseModel):
    kind: Literal["Tool"] = "Tool"
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    tool_call: ToolCall
    decision: ApprovalKind | None = None
    content: ToolContent
    model_config = ConfigDict(extra="forbid")


DisplayItem = Annotated[UserMessageItem | AssistantMarkdownItem | EndTurnItem | ToolItem, Field(discriminator="kind")]


class UiState(BaseModel):
    """Authoritative UI state (server-owned).

    seq: monotonic sequence number incremented with every change
    items: ordered list of display items rendered by the client
    """

    seq: int = 0
    items: list[DisplayItem] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


# ---- Helper functions (pure) ----


def new_state() -> UiState:
    return UiState(seq=0, items=[])


def append_item(state: UiState, item: DisplayItem) -> UiState:
    return UiState(seq=state.seq + 1, items=[*state.items, item])


def start_tool(state: UiState, *, tool_call: ToolCall, cmd: str | None, args: Any | None) -> UiState:
    content: ToolContent = ExecContent(cmd=cmd, args=args) if cmd is not None else JsonContent(args=args)
    return append_item(state, ToolItem(tool_call=tool_call, content=content))


def _find_last_tool_index(state: UiState, call_id: str) -> int | None:
    for idx, it in reversed(list(enumerate(state.items))):
        if isinstance(it, ToolItem) and it.tool_call.call_id == call_id:
            return idx
    return None


def update_tool_decision(state: UiState, call_id: str, decision: ApprovalKind | None) -> UiState:
    idx = _find_last_tool_index(state, call_id)
    if idx is None:
        return state
    it = state.items[idx]
    assert isinstance(it, ToolItem)
    updated = it.model_copy(update={"decision": decision})
    items = list(state.items)
    items[idx] = updated
    return UiState(seq=state.seq + 1, items=items)


def update_tool_exec_stream(
    state: UiState,
    call_id: str,
    *,
    stdout: str | None,
    stderr: str | None,
    exit_code: int | None,
    is_error: bool | None = None,
) -> UiState:
    idx = _find_last_tool_index(state, call_id)
    if idx is None:
        return state
    it = state.items[idx]
    assert isinstance(it, ToolItem)
    content = it.content
    if isinstance(content, ExecContent):
        content = content.model_copy(
            update={
                "stdout": stdout if stdout is not None else content.stdout,
                "stderr": stderr if stderr is not None else content.stderr,
                "exit_code": exit_code if exit_code is not None else content.exit_code,
                "is_error": is_error if is_error is not None else content.is_error,
            }
        )
        updated = it.model_copy(update={"content": content})
        items = list(state.items)
        items[idx] = updated
        return UiState(seq=state.seq + 1, items=items)
    return state


def update_tool_json_output(
    state: UiState, call_id: str, *, result: mcp_types.CallToolResult | None, is_error: bool | None
) -> UiState:
    idx = _find_last_tool_index(state, call_id)
    if idx is None:
        return state
    it = state.items[idx]
    assert isinstance(it, ToolItem)
    content = it.content
    if isinstance(content, JsonContent):
        content = content.model_copy(
            update={
                "result": result if result is not None else content.result,
                "is_error": is_error if is_error is not None else content.is_error,
            }
        )
        updated = it.model_copy(update={"content": content})
        items = list(state.items)
        items[idx] = updated
        return UiState(seq=state.seq + 1, items=items)
    return state
