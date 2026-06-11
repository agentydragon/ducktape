"""Generator-based agent handler for scripted bootstrap sequences.

ScriptHandler wraps a ScriptGen generator into a BaseHandler, analogous to how
GeneratorRunner wraps a PlayGen generator into an OpenAIModelProto. The generator
yields agent actions (tool calls, messages) and receives transcript events
(tool results) in batches.

Generator protocol (batch semantics)::

    yield: Sequence[ScriptItem] | None  →  items to inject (None = no action)
    send:  list[ScriptEvent]        →  events since last yield

- First yield must be None (prime yield — receive pre-existing events)
- StopIteration (generator returns) → handler becomes passive (NoAction forever)
- yield from composes sub-generators for reusable patterns

Usage::

    from agent_core.script_handler import ScriptBuilder, ScriptGen, ScriptHandler

    def my_bootstrap(b: ScriptBuilder, runtime: Mounted[ContainerExecServer]) -> ScriptGen:
        yield None  # prime
        result = yield from b.exec_ok(runtime, ["echo", "hello"])
        print(result.stdout)

    handlers = [ScriptHandler(my_bootstrap(b, runtime)), ...]

Or with the decorator::

    @script_handler
    def my_bootstrap(runtime: Mounted[ContainerExecServer]) -> ScriptGen:
        yield None
        ...

    handlers = [my_bootstrap(runtime), ...]
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Generator, Sequence
from typing import TYPE_CHECKING, Any

from more_itertools import one
from pydantic import BaseModel, TypeAdapter

from agent_core.events import ToolCallOutput
from agent_core.handler import BaseHandler
from agent_core.loop_control import InjectItems, LoopDecision, NoAction
from agent_core.tool_provider import ToolResult
from mcp_infra.exec.models import BaseExecResult, Exited
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.builders import ItemFactory
from openai_utils.model import FunctionCallItem, SystemMessage, UserMessage

if TYPE_CHECKING:
    from mcp_infra.exec.docker.server import ContainerExecServer
    from mcp_infra.mounted import Mounted

logger = logging.getLogger(__name__)

# Event that script generators receive after yielding tool calls.
# Currently only ToolCallOutput; extend if scripts need to react to other event types.
ScriptEvent = ToolCallOutput

ScriptItem = SystemMessage | UserMessage | FunctionCallItem

type ScriptGen[T = None] = Generator[
    Sequence[ScriptItem] | None,  # yield
    list[ScriptEvent],  # send
    T,  # return
]


class ScriptError(Exception):
    """Raised when a scripted step fails."""


def find_tool_result(events: list[ScriptEvent], call_id: str) -> ToolResult:
    """Find the single ToolResult for a specific call_id in a batch of events."""
    return one(
        (evt.result for evt in events if evt.call_id == call_id),
        too_short=ValueError(f"No tool result found: {call_id=}, {len(events)=}"),
        too_long=ValueError(f"Multiple tool results: {call_id=}"),
    )


def find_tool_result_typed[T: BaseModel](events: list[ScriptEvent], call_id: str, output_type: type[T]) -> T:
    """Find and parse typed structured content for a call_id."""
    result = find_tool_result(events, call_id)
    if result.is_error:
        raise ScriptError(f"Tool returned error: {call_id=}, {result=}")
    if not result.structured_content:
        raise ScriptError(f"Tool returned no structured content: {call_id=}")
    return TypeAdapter(output_type).validate_python(result.structured_content)


def script_handler[**P](fn: Callable[P, ScriptGen]) -> Callable[P, ScriptHandler]:
    """Decorator to wrap a generator function into a ScriptHandler factory.

    Usage::

        @script_handler
        def my_bootstrap(runtime: Mounted[ContainerExecServer]) -> ScriptGen:
            yield None  # prime
            result = yield from b.exec_ok(runtime, ["echo", "hello"])

        handlers = [my_bootstrap(runtime), ...]
    """

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> ScriptHandler:
        return ScriptHandler(fn(*args, **kwargs))

    return wrapper


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
        self._events: list[ScriptEvent] = []
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


class ScriptBuilder(ItemFactory):
    """Item factory for ScriptHandler generators.

    Provides call builders (creating FunctionCallItem instances) and
    yield-from-composable roundtrip sub-generators that yield a call,
    wait for its result, and return the parsed output.

    Usage in a script generator::

        def my_bootstrap(b: ScriptBuilder, runtime: Mounted[ContainerExecServer]) -> ScriptGen:
            events = yield None  # prime
            result = yield from b.exec_ok(runtime, ["echo", "hello"])
            print(result.stdout)
    """

    def call(self, server: MCPMountPrefix, tool: str, payload: dict[str, Any] | BaseModel) -> FunctionCallItem:
        """Create a namespaced MCP tool call item."""
        return self.tool_call(build_mcp_function(server, tool), payload)

    def exec_ok(
        self, runtime: Mounted[ContainerExecServer], cmd: list[str], *, timeout_ms: int | None = None
    ) -> ScriptGen[BaseExecResult]:
        """Yield docker exec call, validate exit 0, return result. Defaults to 1000ms timeout."""
        payload = {"cmd": cmd, "timeout_ms": timeout_ms or 1000}
        call = self.call(runtime.prefix, runtime.server.exec_tool.name, payload)
        events: list[ScriptEvent] = yield [call]
        result = find_tool_result_typed(events, call.call_id, BaseExecResult)
        if not (isinstance(result.exit, Exited) and result.exit.exit_code == 0):
            cmd_preview = " ".join(cmd[:4])
            raise ScriptError(
                f"Command failed ({cmd_preview}): {result.exit.model_dump()}"
                f"\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result
