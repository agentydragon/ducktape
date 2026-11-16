"""Event handlers used by the Agent loop."""

from __future__ import annotations

from skills.info_gathering.evals.agent_runtime.events import AssistantText, ToolCall


class BaseHandler:
    """No-op event hooks; keep overrides fast and nonblocking; exceptions propagate."""

    def on_response(self, model: str) -> None:
        """Called after receiving a complete model response."""
        return

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        return

    def on_tool_call_event(self, evt: ToolCall) -> None:
        return
