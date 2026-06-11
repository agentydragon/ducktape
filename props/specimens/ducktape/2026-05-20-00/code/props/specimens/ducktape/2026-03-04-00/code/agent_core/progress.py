"""Simple progress handler for agent execution."""

from __future__ import annotations

from itertools import cycle
from time import monotonic

from rich.console import Console
from rich.text import Text

from agent_core.events import AssistantText, ToolCall, ToolCallOutput, UserText
from agent_core.handler import BaseHandler
from openai_utils.model import ReasoningItem


class OneLineProgressHandler(BaseHandler):
    """Minimal one-line progress handler.

    Displays a single-line status with a spinner, number of tool calls, and a brief
    last action. Non-blocking: updates are printed inline using carriage return.

    Usage: register as a handler in Agent.create(..., handlers=[BaseHandler(), OneLineProgressHandler()])
    """

    def __init__(self, *, console: Console | None = None) -> None:
        self.console = console or Console()
        self._tool_calls = 0
        self._last_action = "(idle)"
        self._spinner = cycle(["-", "\\", "|", "/"])  # simple spinner
        # Track last render time to avoid noisy updates; allow manual forcing via _render()
        self._last_render = 0.0

    # --- observer hooks (fast, non-blocking) ---
    def on_tool_call_event(self, evt: ToolCall) -> None:
        self._tool_calls += 1
        self._last_action = f"tool:{evt.name}"
        self._render()

    def on_tool_result_event(self, evt: ToolCallOutput) -> None:
        self._last_action = f"fco:{evt.call_id}"
        self._render()

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        # Keep last assistant snippet short
        excerpt = evt.text.strip().replace("\n", " ")[:40]
        self._last_action = f"assistant:{excerpt}"
        self._render()

    def on_user_text_event(self, evt: UserText) -> None:
        self._last_action = "user"
        self._render()

    def on_reasoning(self, item: ReasoningItem) -> None:
        self._last_action = "reasoning"
        self._render()

    # --- rendering ---
    def _render(self, force: bool = False) -> None:
        # Throttle updates to ~20Hz to avoid noisy consoles
        now = monotonic()
        if not force and now - self._last_render < 0.05:
            return
        self._last_render = now
        s = next(self._spinner)
        txt = Text(f"{s} tools={self._tool_calls} last={self._last_action}")
        # Print without newline and flush; rely on carriage return
        self.console.print(txt, end="\r")
