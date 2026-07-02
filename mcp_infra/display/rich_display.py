"""Rich-based display handler that renders events immediately."""

from __future__ import annotations

import logging
import shlex
from typing import TYPE_CHECKING, Any

from compact_json import Formatter
from pydantic import TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python
from rich import box
from rich.console import Console, ConsoleOptions, RenderableType
from rich.json import JSON
from rich.panel import Panel
from rich.segment import Segment
from rich.text import Text

from agent_core.events import AssistantText, Response, ToolCall, ToolCallOutput, UserText
from agent_core.handler import BaseHandler
from mcp_infra.display.json_utils import parse_json_or_none
from mcp_infra.display.result_utils import extract_display_data
from mcp_infra.exec.models import BaseExecResult, ExecStream, TruncatedStream
from mcp_infra.naming import parse_tool_name
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.tool_schemas import extract_tool_input_schemas, extract_tool_schemas
from openai_utils.model import ReasoningItem

if TYPE_CHECKING:
    from fastmcp.server import FastMCP

    from mcp_infra.compositor.compositor import Compositor

logger = logging.getLogger(__name__)

DEFAULT_MAX_LINES = 20  # Default maximum lines for rendered content (both Panel height and truncation)

TurnEventType = UserText | AssistantText | ToolCall | ToolCallOutput | ReasoningItem


def _unwrap_shell_command(cmd: list[str]) -> str | None:
    """Unwrap shell -c/-lc wrappers, returning the actual command or None."""
    match cmd:
        case ["bash" | "sh" | "/bin/sh", "-c" | "-lc", actual_cmd]:
            return actual_cmd
        case _:
            return None


class MaxHeight:
    """Wrapper that constrains any renderable to max height without padding.

    Renders the inner content and truncates to max_height lines if needed.
    Short content stays compact (no padding to fill max_height).
    """

    def __init__(self, renderable: RenderableType, max_height: int):
        self.renderable = renderable
        self.max_height = max_height

    def __rich_console__(self, console: Console, options: ConsoleOptions):
        segments = list(console.render(self.renderable, options))

        # Split segments into lines (segments ending with \n are line breaks)
        lines: list[list[Segment]] = []
        current_line: list[Segment] = []

        for segment in segments:
            if "\n" in segment.text:
                parts = segment.text.split("\n")
                for i, part in enumerate(parts):
                    if part:
                        current_line.append(Segment(part, segment.style))
                    if i < len(parts) - 1:
                        current_line.append(Segment("\n", segment.style))
                        lines.append(current_line)
                        current_line = []
            else:
                current_line.append(segment)

        # Don't forget the last line if it doesn't end with newline
        if current_line:
            lines.append(current_line)

        if len(lines) <= self.max_height:
            yield from segments
        else:
            for line in lines[: self.max_height]:
                yield from line
            yield Segment(f"... ({len(lines) - self.max_height} more lines)\n")


class RichDisplayHandler(BaseHandler):
    """Rich-based display handler that renders events immediately.

    Renders each event as it arrives with Rich formatting.
    Each event is limited to max_lines (controls both Panel height and content truncation).

    Automatically extracts tool result schemas from FastMCP servers.
    """

    def __init__(
        self,
        *,
        max_lines: int = DEFAULT_MAX_LINES,
        console: Console | None = None,
        prefix: str = "",
        servers: dict[MCPMountPrefix, FastMCP] | None = None,
    ) -> None:
        self._max_lines = max_lines
        self._console = console or Console()
        self._prefix = prefix

        if servers:
            self._tool_input_schemas = extract_tool_input_schemas(servers)
            self._tool_schemas = extract_tool_schemas(servers)
        else:
            self._tool_input_schemas = {}
            self._tool_schemas = {}

        self._calls: dict[str, ToolCall] = {}

    @classmethod
    async def from_compositor(
        cls,
        compositor: Compositor,
        *,
        max_lines: int = DEFAULT_MAX_LINES,
        console: Console | None = None,
        prefix: str = "",
    ) -> RichDisplayHandler:
        """Create handler with schemas extracted from compositor."""
        servers = await compositor.get_inproc_servers()
        return cls(max_lines=max_lines, console=console, prefix=prefix, servers=servers)

    # Observer hooks ---------------------------------------------------------

    def on_user_text_event(self, evt: UserText) -> None:
        self._render_event(evt)

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        self._render_event(evt)

    def on_tool_call_event(self, evt: ToolCall) -> None:
        self._calls[evt.call_id] = evt
        self._render_event(evt)

    def on_tool_result_event(self, evt: ToolCallOutput) -> None:
        self._render_event(evt)

    def on_reasoning(self, item: ReasoningItem) -> None:
        self._render_event(item)

    def on_response(self, evt: Response) -> None:
        pass  # No buffering, events already rendered

    # Rendering --------------------------------------------------------------

    def _render_event(self, event: TurnEventType) -> None:
        renderable = self._create_renderable(event)
        self._console.print(renderable)

    def _panel(
        self, content: RenderableType, title: str, color: str, *, bold: bool = False, border: bool = False
    ) -> Panel:
        """Create a Panel with colored title and height-constrained content.

        Uses MaxHeight wrapper to truncate tall content while keeping short content compact.
        Uses horizontal-only box (no left/right borders).
        Prepends prefix to title.
        """
        style = f"bold {color}" if bold else color
        # Incorporate prefix into title as a Text object (no markup interpretation)
        full_title = f"{self._prefix}{title}" if self._prefix else title
        formatted_title = Text(full_title, style=style)

        constrained_content = MaxHeight(content, self._max_lines)

        kwargs: dict[str, object] = {"title": formatted_title, "box": box.HORIZONTALS}
        if border:
            kwargs["border_style"] = color
        return Panel(constrained_content, **kwargs)  # type: ignore[arg-type]

    def _format_exec_input(self, input_data: Any) -> Text:
        lines = []

        timeout_sec = input_data.timeout_ms / 1000
        lines.append(f"Timeout: {timeout_sec:.1f}s")

        unwrapped = _unwrap_shell_command(input_data.cmd)
        if unwrapped:
            lines.append(unwrapped)
        else:
            quoted = " ".join(shlex.quote(part) for part in input_data.cmd)
            lines.append(quoted)

        return Text("\n".join(lines))

    def _format_exec_result(self, result: BaseExecResult) -> Text:
        lines = []

        exit_status = result.exit
        if exit_status.kind == "exited":
            lines.append(f"Exit: {exit_status.exit_code} | Duration: {result.duration_ms}ms")
        elif exit_status.kind == "killed":
            lines.append(f"Killed: signal {exit_status.signal} | Duration: {result.duration_ms}ms")
        elif exit_status.kind == "timed_out":
            lines.append(f"Exit: timed out | Duration: {result.duration_ms}ms")

        if isinstance(result.stdout, TruncatedStream):
            stdout_text = result.stdout.truncated_text + "\n[truncated]"
        else:
            stdout_text = result.stdout

        if stdout_text:
            lines.append(f"\nStdout:\n{stdout_text}")

        if isinstance(result.stderr, TruncatedStream):
            stderr_text = result.stderr.truncated_text + "\n[truncated]"
        else:
            stderr_text = result.stderr

        if stderr_text:
            lines.append(f"\nStderr:\n{stderr_text}")

        return Text("\n".join(lines))

    def _create_renderable(self, event: TurnEventType) -> RenderableType:
        if isinstance(event, UserText):
            return self._panel(Text(event.text), "User", "blue", bold=True, border=True)

        if isinstance(event, AssistantText):
            return self._panel(Text(event.text), "Assistant", "green", bold=True, border=True)

        if isinstance(event, ToolCall):
            parsed = parse_json_or_none(event.args_json)
            args = parsed if parsed is not None else {"_raw": event.args_json or "{}"}

            tool_key = parse_tool_name(event.name)
            if tool_key in self._tool_input_schemas and parsed is not None:
                try:
                    input_type = self._tool_input_schemas[tool_key]
                    typed_input = TypeAdapter(input_type).validate_python(parsed)

                    # TODO: replace duck-type check with a proper marker (e.g. a
                    # base class or Protocol) so the display layer doesn't guess.
                    if hasattr(typed_input, "cmd") and hasattr(typed_input, "timeout_ms"):
                        formatted = self._format_exec_input(typed_input)
                        return self._panel(formatted, f"▶ {event.name}", "cyan")
                except ValidationError:
                    pass  # Fall through to default JSON rendering

            return self._panel(JSON.from_data(args), f"▶ {event.name}", "cyan")

        if isinstance(event, ToolCallOutput):
            call = self._calls.get(event.call_id)
            label = f"◀ {call.name}" if call else "◀ tool_output"

            display_data: Any = extract_display_data(event.result)

            if isinstance(display_data, dict) and call:
                tool_key = parse_tool_name(call.name)
                if tool_key in self._tool_schemas:
                    try:
                        result_type = self._tool_schemas[tool_key]
                        display_data = TypeAdapter(result_type).validate_python(display_data)
                    except ValidationError:
                        pass  # Keep raw data

            if isinstance(display_data, str):
                return self._panel(Text(display_data), label, "yellow")

            if isinstance(display_data, BaseExecResult):
                formatted = self._format_exec_result(display_data)
                return self._panel(formatted, label, "yellow")

            return self._panel(JSON.from_data(display_data), label, "yellow")

        if isinstance(event, ReasoningItem):
            return Text("💭 reasoning...", style="dim")

        return Text(str(event))


class CompactDisplayHandler(BaseHandler):
    """Compact display handler with Claude Code-style formatting.

    Uses bullet points, indentation, and inline metadata instead of panels.
    Optimized for minimal vertical space while maintaining readability.
    """

    # Display formatting constants
    _TOOL_CALL_INDENT = 2  # Indentation for complex tool call arguments
    _TOOL_RESULT_INDENT = 2  # Indentation for tool result content
    _TOOL_RESULT_PREFIX = "  ⎿ "  # Prefix for tool result lines

    def __init__(
        self,
        *,
        max_lines: int = DEFAULT_MAX_LINES,
        console: Console | None = None,
        prefix: str = "",
        servers: dict[MCPMountPrefix, FastMCP] | None = None,
        show_token_usage: bool = True,
    ) -> None:
        self._max_lines = max_lines
        self._console = console or Console()
        self._prefix = prefix
        self._show_token_usage = show_token_usage

        if servers:
            self._tool_input_schemas = extract_tool_input_schemas(servers)
            self._tool_schemas = extract_tool_schemas(servers)
        else:
            self._tool_input_schemas = {}
            self._tool_schemas = {}

        self._calls: dict[str, ToolCall] = {}

    @classmethod
    async def from_compositor(
        cls,
        compositor: Compositor,
        *,
        max_lines: int = DEFAULT_MAX_LINES,
        console: Console | None = None,
        prefix: str = "",
        show_token_usage: bool = True,
    ) -> CompactDisplayHandler:
        """Create handler with schemas extracted from compositor."""
        servers = await compositor.get_inproc_servers()
        return cls(
            max_lines=max_lines, console=console, prefix=prefix, servers=servers, show_token_usage=show_token_usage
        )

    # Observer hooks ---------------------------------------------------------

    def on_user_text_event(self, evt: UserText) -> None:
        self._render_event(evt)

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        self._render_event(evt)

    def on_tool_call_event(self, evt: ToolCall) -> None:
        self._calls[evt.call_id] = evt
        self._render_event(evt)

    def on_tool_result_event(self, evt: ToolCallOutput) -> None:
        self._render_event(evt)

    def on_reasoning(self, item: ReasoningItem) -> None:
        self._render_event(item)

    def on_response(self, evt: Response) -> None:
        # TODO: Track cost and show it in USD (need model pricing lookup)
        if not self._show_token_usage or not evt.usage:
            return

        input_tok = evt.usage.input_tokens or 0
        output_tok = evt.usage.output_tokens or 0

        text = Text()
        text.append("  [tokens] ", style="dim")
        text.append(f"{self._format_tokens(input_tok)} in / {self._format_tokens(output_tok)} out", style="dim")

        if evt.usage.model:
            text.append(f"  {evt.usage.model}", style="dim italic")

        self._console.print(text)

    # Rendering --------------------------------------------------------------

    def _render_event(self, event: TurnEventType) -> None:
        renderable = self._create_renderable(event)
        if renderable is not None:
            self._console.print(MaxHeight(renderable, self._max_lines))

    def _format_exec_command(self, input_data: Any) -> str:
        """Format exec tool input as a shell command."""
        unwrapped = _unwrap_shell_command(input_data.cmd)
        if unwrapped:
            return unwrapped
        return " ".join(shlex.quote(part) for part in input_data.cmd)

    def _format_exec_metadata(self, result: BaseExecResult) -> str:
        """Format exec result metadata (exit code, duration) for inline display."""
        parts = []

        exit_status = result.exit
        if exit_status.kind == "exited":
            parts.append(f"exit_code={exit_status.exit_code}")
        elif exit_status.kind == "killed":
            parts.append(f"killed=signal_{exit_status.signal}")
        elif exit_status.kind == "timed_out":
            parts.append("exit_code=timeout")

        duration_sec = result.duration_ms / 1000
        parts.append(f"duration={duration_sec:.1f}s")

        return "  ".join(parts)

    def _truncate_lines(self, text: str, max_lines: int, indent: int = 0) -> str:
        """Truncate text to max_lines with indicator.

        Handles both too many lines and individual lines that are too long.
        Long lines are hard-wrapped at console width to prevent Rich from
        word-wrapping them later (which would make MaxHeight less effective).
        """
        lines = text.splitlines()

        max_line_length = self._console.width - indent
        wrapped_lines = []
        for original_line in lines:
            if len(original_line) <= max_line_length:
                wrapped_lines.append(original_line)
            else:
                remaining = original_line
                while remaining:
                    wrapped_lines.append(remaining[:max_line_length])
                    remaining = remaining[max_line_length:]

        if len(wrapped_lines) <= max_lines:
            return "\n".join(wrapped_lines)

        truncated = wrapped_lines[:max_lines]
        return "\n".join(truncated) + f"\n... ({len(wrapped_lines) - max_lines} more lines)"

    def _indent(self, text: str, spaces: int = 2) -> str:
        indent = " " * spaces
        return "\n".join(indent + line for line in text.splitlines())

    def _format_tokens(self, count: int) -> str:
        """Format token count with k suffix for numbers >= 1000."""
        if count >= 1000:
            return f"{count / 1000:.1f}k"
        return str(count)

    def _extract_stream_text(self, stream: str | TruncatedStream) -> str:
        if isinstance(stream, TruncatedStream):
            return stream.truncated_text + "\n[truncated]"
        return stream

    def _try_parse_with_schema(
        self, parsed_data: Any, tool_name: str, schemas: dict[tuple[MCPMountPrefix, str], type]
    ) -> Any:
        """Try to parse data with registered schema, return original on failure."""
        if parsed_data is None:
            return None
        tool_key = parse_tool_name(tool_name)
        if tool_key not in schemas:
            return parsed_data
        try:
            result_type = schemas[tool_key]
            return TypeAdapter(result_type).validate_python(parsed_data)
        except ValidationError:
            return parsed_data

    def _format_prefix_label(self, default: str = "Assistant") -> str:
        return f"{self._prefix}: " if self._prefix else f"{default}: "

    def _create_renderable(self, event: TurnEventType) -> RenderableType | None:
        if isinstance(event, UserText):
            text = Text()
            text.append("User: ", style="bold blue")
            text.append(event.text)
            return text

        if isinstance(event, AssistantText):
            text = Text()
            text.append(self._format_prefix_label(), style="bold green")
            text.append(event.text)
            return text

        if isinstance(event, ReasoningItem):
            if not event.summary:
                return None
            text = Text()
            if self._prefix:
                text.append(self._format_prefix_label(), style="bold green")
            summary_text = " ".join(s.text for s in event.summary if s.text)
            text.append(summary_text, style="italic dim")
            return text

        if isinstance(event, ToolCall):
            parsed = parse_json_or_none(event.args_json)
            args = parsed if parsed is not None else {}

            typed_input = self._try_parse_with_schema(parsed, event.name, self._tool_input_schemas)

            text = Text()
            prefix_part = self._format_prefix_label(default="")
            text.append(f"● {prefix_part}")
            text.append(event.name, style="bold")

            # TODO: replace duck-type check with a proper marker (e.g. a
            # base class or Protocol) so the display layer doesn't guess.
            if hasattr(typed_input, "cmd") and hasattr(typed_input, "timeout_ms"):
                cmd_str = self._format_exec_command(typed_input)
                truncated_cmd = self._truncate_lines(cmd_str, self._max_lines, indent=0)
                text.append(f": {truncated_cmd}")
                cwd = getattr(typed_input, "cwd", None)
                if cwd:
                    text.append(f"  [cwd={cwd}]", style="dim")
            elif args:
                formatter = Formatter(max_inline_length=self._console.width - self._TOOL_CALL_INDENT)
                json_str = formatter.serialize(args)  # type: ignore[arg-type]
                truncated_json = self._truncate_lines(json_str, self._max_lines, indent=self._TOOL_CALL_INDENT)
                if "\n" not in truncated_json and len(truncated_json) < 80:
                    text.append(f": {truncated_json}", style="dim")
                else:
                    text.append("\n")
                    indented = self._indent(truncated_json, self._TOOL_CALL_INDENT)
                    text.append(indented, style="dim")

            return text

        if isinstance(event, ToolCallOutput):
            call = self._calls.get(event.call_id)

            display_data: Any = extract_display_data(event.result)

            if isinstance(display_data, dict) and call:
                display_data = self._try_parse_with_schema(display_data, call.name, self._tool_schemas)

            text = Text()

            if isinstance(display_data, BaseExecResult):
                metadata = self._format_exec_metadata(display_data)
                text.append(self._TOOL_RESULT_PREFIX)
                text.append(metadata, style="dim")

                has_stdout = bool(
                    display_data.stdout.truncated_text
                    if isinstance(display_data.stdout, TruncatedStream)
                    else display_data.stdout
                )
                has_stderr = bool(
                    display_data.stderr.truncated_text
                    if isinstance(display_data.stderr, TruncatedStream)
                    else display_data.stderr
                )
                both_present = has_stdout and has_stderr

                def append_stream(stream: ExecStream, label: str, style: str = ""):
                    stream_text = self._extract_stream_text(stream)
                    truncated = self._truncate_lines(stream_text, self._max_lines, indent=self._TOOL_RESULT_INDENT)
                    if both_present:
                        text.append(f"\n    {label}:\n", style=style)
                    else:
                        text.append("\n")
                    text.append(self._indent(truncated, self._TOOL_RESULT_INDENT), style=style)

                if has_stdout:
                    append_stream(display_data.stdout, "stdout")
                if has_stderr:
                    append_stream(display_data.stderr, "stderr", style="red")

                return text

            # Handle error results (isError flag is serialized in JSON by adapter)
            is_error = isinstance(display_data, dict) and display_data.get("isError")
            if is_error:
                text.append("  ⎿ ✗ ", style="bold red")
                # TODO: Bad pattern - guessing at error structure. Should use typed error models
                # or just display raw structured content.
                error_content = display_data.get("structuredContent", {})
                if isinstance(error_content, dict):
                    error_msg = error_content.get("error") or error_content.get("message") or str(error_content)
                else:
                    error_msg = str(error_content) if error_content else str(display_data)
                truncated = self._truncate_lines(error_msg, self._max_lines, indent=0)
                text.append(truncated, style="red")
                return text

            if isinstance(display_data, str):
                truncated = self._truncate_lines(display_data, self._max_lines, indent=self._TOOL_RESULT_INDENT)
                text.append(self._TOOL_RESULT_PREFIX)
                if "\n" not in truncated:
                    text.append(truncated)
                else:
                    text.append("\n")
                    text.append(self._indent(truncated, self._TOOL_RESULT_INDENT))
                return text

            available_width = self._console.width - len(self._TOOL_RESULT_PREFIX) - self._TOOL_RESULT_INDENT
            formatter = Formatter(max_inline_length=available_width)

            # Ensure display_data is JSON-safe using pydantic_core
            # (handles BaseModel, dicts with Url/AnyUrl, and other Pydantic types)
            display_data = to_jsonable_python(display_data)

            json_str = formatter.serialize(display_data)
            truncated = self._truncate_lines(json_str, self._max_lines, indent=len(self._TOOL_RESULT_PREFIX))
            lines = truncated.splitlines()
            if lines:
                text.append(self._TOOL_RESULT_PREFIX)
                text.append(lines[0] + "\n", style="dim")
                if len(lines) > 1:
                    text.append(self._indent("\n".join(lines[1:]), self._TOOL_RESULT_INDENT), style="dim")
            return text

        return Text(str(event))
