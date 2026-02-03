"""Item factory and roundtrip helpers for ScriptHandler generators.

ScriptBuilder inherits from ItemFactory and absorbs the functionality of
TypedBootstrapBuilder and docker_exec_call(). It provides:

- Call builders: call(), docker_exec(), read_resource()
- Roundtrip sub-generators (for yield from): exec_roundtrip(), exec_ok(), call_roundtrip()
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import pydantic_core
from pydantic import BaseModel
from pydantic.networks import AnyUrl

from agent_core.events import TranscriptEvent
from agent_core.script_handler import ScriptError, ScriptGen, find_tool_result_typed
from mcp_infra.compositor.resources_server import ResourcesReadArgs, ResourcesServer
from mcp_infra.exec.models import BaseExecResult, ExecInput, Exited
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.builders import ItemFactory
from openai_utils.model import FunctionCallItem

if TYPE_CHECKING:
    from mcp_infra.exec.docker.server import ContainerExecServer
    from mcp_infra.mounted import Mounted

__all__ = ["ScriptBuilder"]

DEFAULT_BOOTSTRAP_EXEC_TIMEOUT_MS = 1000


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

    def __init__(self, *, call_id_prefix: str = "bootstrap") -> None:
        super().__init__(call_id_prefix=call_id_prefix)

    def call(
        self, server: MCPMountPrefix, tool: str, payload: BaseModel, *, call_id: str | None = None
    ) -> FunctionCallItem:
        """Create a namespaced MCP tool call item."""
        return FunctionCallItem(
            call_id=call_id or self.next_call_id(),
            name=build_mcp_function(server, tool),
            arguments=pydantic_core.to_json(payload.model_dump(mode="json"), fallback=str).decode("utf-8"),
        )

    def docker_exec(
        self, runtime: Mounted[ContainerExecServer], cmd: Sequence[str | Path], *, timeout_ms: int | None = None
    ) -> FunctionCallItem:
        """Create a docker exec tool call item.

        Uses DEFAULT_BOOTSTRAP_EXEC_TIMEOUT_MS (1 second) by default.
        """
        cmd_str = [str(item) for item in cmd]
        return self.call(
            runtime.prefix,
            runtime.server.exec_tool.name,
            ExecInput(
                cmd=cmd_str, cwd=None, env=None, user=None, timeout_ms=timeout_ms or DEFAULT_BOOTSTRAP_EXEC_TIMEOUT_MS
            ),
        )

    def read_resource(
        self, resources: Mounted[ResourcesServer], server: MCPMountPrefix, uri: str | AnyUrl, *, max_bytes: int = 65536
    ) -> FunctionCallItem:
        """Create a resource read tool call item."""
        return self.call(
            resources.prefix,
            resources.server.read_tool.name,
            ResourcesReadArgs(server=server, uri=str(uri), start_offset=0, max_bytes=max_bytes),
        )

    def exec_roundtrip(
        self, runtime: Mounted[ContainerExecServer], cmd: Sequence[str | Path], *, timeout_ms: int | None = None
    ) -> ScriptGen[BaseExecResult]:
        """Yield docker exec call, return BaseExecResult."""
        call = self.docker_exec(runtime, cmd, timeout_ms=timeout_ms)
        events: list[TranscriptEvent] = yield [call]
        return find_tool_result_typed(events, call.call_id, BaseExecResult)

    def exec_ok(
        self, runtime: Mounted[ContainerExecServer], cmd: Sequence[str | Path], *, timeout_ms: int | None = None
    ) -> ScriptGen[BaseExecResult]:
        """Yield docker exec call, validate exit 0, return result."""
        result: BaseExecResult = yield from self.exec_roundtrip(runtime, cmd, timeout_ms=timeout_ms)
        _validate_exit_zero(result, cmd)
        return result

    def call_roundtrip[T: BaseModel](
        self, server: MCPMountPrefix, tool: str, payload: BaseModel, output_type: type[T]
    ) -> ScriptGen[T]:
        """Yield MCP tool call, return parsed typed output."""
        call = self.call(server, tool, payload)
        events: list[TranscriptEvent] = yield [call]
        return find_tool_result_typed(events, call.call_id, output_type)


def _validate_exit_zero(result: BaseExecResult, cmd: Sequence[str | Path]) -> None:
    """Validate that an exec result exited with code 0."""
    if not (isinstance(result.exit, Exited) and result.exit.exit_code == 0):
        cmd_preview = " ".join(str(c) for c in cmd[:4])
        raise ScriptError(f"Command failed ({cmd_preview}): {result.exit.model_dump()}")
