from __future__ import annotations

import shlex
from collections.abc import Callable

# Conditional import to avoid circular dependency when compositor is not available
from typing import TYPE_CHECKING, Any, get_args, get_type_hints

import pydantic_core
from fastmcp.tools.tool import FunctionTool
from mcp import types as mcp_types

from mcp_infra.exec.models import ExecInput
from mcp_infra.naming import parse_tool_name
from openai_utils.model import ReasoningItem

if TYPE_CHECKING:
    from mcp_infra.compositor.server import Compositor

from agent_core.events import AssistantText, ToolCall, ToolCallOutput, UserText
from agent_core.handler import BaseHandler

from .json_utils import parse_json_or_none


def _extract_display_data(result: mcp_types.CallToolResult) -> Any:
    """Extract display-friendly data from mcp.types.CallToolResult.

    Prefers structuredContent if present, otherwise extracts text from content blocks.
    """
    if result.structuredContent is not None:
        return result.structuredContent
    texts = [block.text for block in result.content if isinstance(block, mcp_types.TextContent)]
    if texts:
        return "\n".join(texts) if len(texts) > 1 else texts[0]
    return None


class DisplayEventsHandler(BaseHandler):
    """Handler that prints agent events directly (no separate renderer class).

    Configure by constructor args; omit this handler from the stack to suppress
    console output entirely.
    """

    def __init__(
        self,
        *,
        compositor: Compositor | None = None,
        max_lines: int = 200,
        max_bytes: int = 8192,
        write: Callable[[str], None] = print,
        prefix: str = "",
    ) -> None:
        self._compositor = compositor
        self._max_lines = max_lines
        self._max_bytes = max_bytes
        self._write = write
        self._prefix = prefix
        self._calls: dict[str, ToolCall] = {}

    # Type introspection helper ----------------------------------------------

    def _get_tool_input_type(self, call_name: str) -> type | None:
        """Extract actual Pydantic input type from tool function.

        Returns the input parameter type of the tool, or None if:
        - No compositor available
        - Server is external (not inproc)
        - Tool is not a FunctionTool

        Raises ValueError if tool name doesn't follow compositor format (server_tool).
        Raises if tool isn't found or type hints are broken (these indicate bugs).
        """
        if self._compositor is None:
            return None

        prefix, tool_name = parse_tool_name(call_name)

        server = self._compositor.get_inproc_server(prefix)
        if server is None:
            return None

        # Use synchronous tool access via _tool_manager
        tool = server._tool_manager.get_tool(tool_name)

        if not isinstance(tool, FunctionTool):
            return None

        hints = get_type_hints(tool.fn)
        params = list(hints.values())
        if not params:
            return None

        input_type = params[0]

        # Unwrap Annotated, Optional, etc.
        if hasattr(input_type, "__origin__"):
            args = get_args(input_type)
            if args:
                input_type = args[0]

        return input_type if isinstance(input_type, type) else None

    # Observer hooks ---------------------------------------------------------

    def on_user_text_event(self, evt: UserText) -> None:
        if evt.text:
            self._write_with_prefix(f"user:\n{self._truncate_text(evt.text)}")

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        if evt.text:
            self._write_with_prefix(f"assistant:\n{self._truncate_text(evt.text)}")

    def on_tool_call_event(self, evt: ToolCall) -> None:
        self._calls[evt.call_id] = evt

        # Type-based dispatch
        input_type = self._get_tool_input_type(evt.name)

        if input_type is ExecInput:
            # Specialized exec rendering
            call_args = parse_json_or_none(evt.args_json) or {}
            if isinstance(call_args, dict) and (cmd := call_args.get("cmd")) is not None:
                cmd_line = shlex.join([str(x) for x in cmd]) if isinstance(cmd, list) else str(cmd)
                self._write_with_prefix(f"$ {cmd_line}")
            return

        # Default rendering for other tools
        s = self._render_tool_call(evt)
        if s:
            self._write_with_prefix(s)

    def on_tool_result_event(self, evt: ToolCallOutput) -> None:
        call = self._calls.get(evt.call_id)

        if call:
            # Type-based dispatch
            input_type = self._get_tool_input_type(call.name)

            if input_type is ExecInput:
                # Extract result data from ToolOutput
                data: Any = _extract_display_data(evt.result)

                s = self._render_docker_exec(call.name, call, data)
                if s:
                    self._write_with_prefix(s)
                return

        # Default rendering
        s = self._render_tool_result(call, evt)
        if s:
            self._write_with_prefix(s)

    def on_reasoning(self, item: ReasoningItem) -> None:
        return None

    # Rendering helpers ------------------------------------------------------

    def _render_tool_call(self, tc: ToolCall) -> str:
        # Require a valid namespaced tool name; do not synthesize placeholders
        if not tc.name:
            raise ValueError("ToolCall.name must be a non-empty namespaced tool name")
        name = tc.name
        parsed: Any | None = parse_json_or_none(tc.args_json)
        args = parsed if parsed is not None else ({"_raw": tc.args_json} if tc.args_json else {})
        header = f"▶ {name} input:"
        return f"{header}\n{self._pp_json(args)}"

    def _render_tool_result(self, call: ToolCall | None, output: ToolCallOutput) -> str:
        data: Any = _extract_display_data(output.result)

        # Default rendering (specialized rendering is handled in on_tool_result_event)
        label = call.name if call is not None else "tool_output"
        if isinstance(data, str):
            return f"◀ {label}:\n{data}"
        return f"◀ {label}:\n{self._pp_json(data)}"

    def _render_docker_exec(self, name: str, call: ToolCall, data: object) -> str:
        # Prefer structured keys when available; otherwise fall back to pretty JSON
        if not isinstance(data, dict):
            return f"$ <{name}>\n{self._pp_json(data)}"
        exit_code = data.get("exit_code")
        timed_out = data.get("timed_out")
        header_bits: list[str] = []
        if exit_code is not None:
            header_bits.append(f"exit {exit_code}")
        if timed_out:
            header_bits.append("timeout true")
        header = "[" + ", ".join(header_bits) + "]" if header_bits else ""

        out_parts: list[str] = []
        if header:
            out_parts.append(header)
        if stdout := data.get("stdout"):
            out_parts.append("stdout:")
            out_parts.append(self._truncate_text(_coerce_str(stdout)))
        if (stderr := data.get("stderr")) and stderr:
            out_parts.append("stderr:")
            out_parts.append(self._truncate_text(_coerce_str(stderr)))
        return "\n".join(out_parts)

    # Utility methods --------------------------------------------------------

    def _write_with_prefix(self, msg: str) -> None:
        """Write message with optional prefix."""
        if self._prefix:
            # Prefix each line of the message
            lines = msg.splitlines()
            prefixed = "\n".join(f"{self._prefix}{line}" for line in lines)
            self._write(prefixed)
        else:
            self._write(msg)

    def _truncate_text(self, s: str) -> str:
        raw = s.encode("utf-8", errors="replace")
        if len(raw) > self._max_bytes:
            raw = raw[: self._max_bytes]
            s = raw.decode("utf-8", errors="replace")
            s += f"\n… truncated (+{len(s.encode('utf-8')) - len(raw)} bytes)"
        lines = s.splitlines()
        if len(lines) > self._max_lines:
            kept = lines[: self._max_lines]
            s = "\n".join(kept) + f"\n… truncated (+{len(lines) - self._max_lines} lines)"
        return s

    def _pp_json(self, obj: object) -> str:
        try:
            text = pydantic_core.to_json(obj, indent=2, fallback=str).decode("utf-8")
        except (TypeError, ValueError):
            text = str(obj)
        return self._truncate_text(text)


def _coerce_str(x: object) -> str:
    if isinstance(x, str):
        return x
    try:
        return pydantic_core.to_json(x, fallback=str).decode("utf-8")
    except (TypeError, ValueError):
        return str(x)
