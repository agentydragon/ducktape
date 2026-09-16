"""Explicit native Claude stream/control frame constructors; no neutral facade."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from x.agentplane.native.claude import wire


def initialize(
    *,
    hooks: dict[str, list[str]] | None = None,
    instructions: str = "",
    sdk_mcp_servers: list[str] | None = None,
    sdk_mcp_server_configs: dict[str, wire.SdkMcpServerConfig] | None = None,
) -> wire.InitializeRequest:
    """`hooks` maps a hook event to the callback ids answering it; each firing is a `hook_callback`.

    `instructions` is appended to the harness's system prompt for every turn of the session; empty
    leaves the prompt as the harness builds it, and sends no key for it. `sdk_mcp_servers` names the
    driver-hosted MCP servers to declare; the CLI then speaks MCP to the driver over `mcp_message`.
    """
    return wire.InitializeRequest(
        request=wire.InitializeBody(
            hooks=(
                None
                if hooks is None
                else {event: [wire.HookMatcher(hook_callback_ids=ids)] for event, ids in hooks.items()}
            ),
            append_system_prompt=instructions or None,
            sdk_mcp_servers=sdk_mcp_servers,
            sdk_mcp_server_configs=sdk_mcp_server_configs,
        )
    )


def mcp_response(request_id: str, message: dict[str, Any]) -> wire.ControlResponse:
    """The answer to one `mcp_message`: `message` is the JSON-RPC reply, including for a JSON-RPC
    notification, which still needs one or the CLI's handshake times out."""
    return wire.ControlResponse(
        response=wire.ControlResponseBody(subtype="success", request_id=request_id, response={"mcp_response": message})
    )


def allow_tool(request_id: str, tool_input: dict[str, Any]) -> wire.ControlResponse:
    """The answer to a `can_use_tool` that lets the call through unchanged."""
    return wire.ControlResponse(
        response=wire.ControlResponseBody(
            subtype="success", request_id=request_id, response={"behavior": "allow", "updatedInput": tool_input}
        )
    )


def hook_output(request_id: str, output: dict[str, Any]) -> wire.ControlResponse:
    """The answer to a `hook_callback`: the hook's JSON output, `{}` for "no opinion"."""
    return wire.ControlResponse(
        response=wire.ControlResponseBody(subtype="success", request_id=request_id, response=output)
    )


def user_frame(text: str, *, message_uuid: str | None = None) -> wire.UserInput:
    return wire.UserInput(message=wire.UserMessage(role="user", content=text), uuid=message_uuid or str(uuid4()))


def interrupt(*, cancel_queued: bool, reason: str = "capture") -> wire.InterruptRequest:
    return wire.InterruptRequest(request=wire.InterruptBody(reason=reason, cancel_queued=cancel_queued))


def set_model(model: str) -> wire.SetModelRequest:
    return wire.SetModelRequest(request=wire.SetModelBody(model=model))
