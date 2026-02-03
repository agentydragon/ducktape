"""Generator-based agent handler for scripted bootstrap sequences.

ScriptHandler wraps a ScriptGen generator into a BaseHandler, analogous to how
GeneratorRunner wraps a PlayGen generator into an OpenAIModelProto. The generator
yields agent actions (tool calls, messages) and receives transcript events
(tool results) in batches.

Generator protocol (batch semantics):
- yield: Sequence[ScriptItem] | None  →  items to inject (None = no action)
- send:  list[TranscriptEvent]        →  events since last yield
- First yield must be None (prime yield — receive pre-existing events)
- StopIteration (generator returns) → handler becomes passive (NoAction forever)
- yield from composes sub-generators for reusable patterns
"""

from __future__ import annotations

import logging
from collections.abc import Generator, Sequence

from more_itertools import one
from pydantic import BaseModel, TypeAdapter

from agent_core.events import ToolCallOutput, TranscriptEvent
from agent_core.handler import BaseHandler
from agent_core.loop_control import InjectItems, LoopDecision, NoAction
from agent_core.tool_provider import ToolResult
from openai_utils.model import FunctionCallItem, SystemMessage, UserMessage

logger = logging.getLogger(__name__)

__all__ = ["ScriptError", "ScriptGen", "ScriptHandler", "ScriptItem", "find_tool_result", "find_tool_result_typed"]

ScriptItem = SystemMessage | UserMessage | FunctionCallItem

type ScriptGen[T = None] = Generator[
    Sequence[ScriptItem] | None,  # yield
    list[TranscriptEvent],  # send
    T,  # return
]


class ScriptError(Exception):
    """Raised when a scripted step fails."""


def find_tool_result(events: list[TranscriptEvent], call_id: str) -> ToolResult:
    """Find the single ToolResult for a specific call_id in a batch of events."""
    return one(
        (evt.result for evt in events if evt.call_id == call_id),
        too_short=ValueError(f"No tool result found: {call_id=}, {len(events)=}"),
        too_long=ValueError(f"Multiple tool results: {call_id=}"),
    )


def find_tool_result_typed[T: BaseModel](events: list[TranscriptEvent], call_id: str, output_type: type[T]) -> T:
    """Find and parse typed structured content for a call_id."""
    result = find_tool_result(events, call_id)
    if result.is_error:
        raise ScriptError(f"Tool returned error: {call_id=}, {result=}")
    if not result.structured_content:
        raise ScriptError(f"Tool returned no structured content: {call_id=}")
    return TypeAdapter(output_type).validate_python(result.structured_content)


class ScriptHandler(BaseHandler):
    """Wrap a ScriptGen generator into a BaseHandler.

    Mirrors GeneratorRunner: primes the generator with next(), translates
    between BaseHandler hooks and the generator protocol.

    - on_tool_result_event(evt) → buffer event
    - on_before_sample() → send(buffered_events) to generator, wrap yielded
      items into InjectItems, or NoAction() if generator returned None or
      is exhausted
    """

    def __init__(self, gen: ScriptGen) -> None:
        self._gen = gen
        self._events: list[TranscriptEvent] = []
        self._exhausted = False

        # Prime: advance to first yield and assert it's None
        prime = next(self._gen)
        if prime is not None:
            raise RuntimeError("ScriptGen first yield must be None (prime yield)")

    def on_tool_result_event(self, evt: ToolCallOutput) -> None:
        if not self._exhausted:
            self._events.append(evt)

    def on_before_sample(self) -> LoopDecision:
        if self._exhausted:
            return NoAction()

        events = self._events
        self._events = []

        try:
            items = self._gen.send(events)
        except StopIteration:
            self._exhausted = True
            return NoAction()

        if items is None:
            return NoAction()

        return InjectItems(items=list(items))
