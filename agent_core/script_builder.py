"""Item factory and roundtrip helpers for ScriptHandler generators."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from agent_core.events import ScriptEvent
from agent_core.script_handler import ScriptError, ScriptGen, find_tool_result_typed
from mcp_infra.exec.models import BaseExecResult, ExecInput, Exited
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.builders import ItemFactory
from openai_utils.model import FunctionCallItem

if TYPE_CHECKING:
    from mcp_infra.exec.docker.server import ContainerExecServer
    from mcp_infra.mounted import Mounted


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

    def call(self, server: MCPMountPrefix, tool: str, payload: BaseModel) -> FunctionCallItem:
        """Create a namespaced MCP tool call item."""
        return self.tool_call(build_mcp_function(server, tool), payload)

    def exec_ok(
        self, runtime: Mounted[ContainerExecServer], cmd: list[str], *, timeout_ms: int | None = None
    ) -> ScriptGen[BaseExecResult]:
        """Yield docker exec call, validate exit 0, return result. Defaults to 1000ms timeout."""
        call = self.call(
            runtime.prefix,
            runtime.server.exec_tool.name,
            ExecInput(cmd=cmd, cwd=None, env=None, user=None, timeout_ms=timeout_ms or 1000),
        )
        events: list[ScriptEvent] = yield [call]
        result = find_tool_result_typed(events, call.call_id, BaseExecResult)
        if not (isinstance(result.exit, Exited) and result.exit.exit_code == 0):
            cmd_preview = " ".join(cmd[:4])
            raise ScriptError(f"Command failed ({cmd_preview}): {result.exit.model_dump()}")
        return result
