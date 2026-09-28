"""Configured Actions as MCP tools of their own, for callers on an external Connection.

A group's reviewed `direct_tools` names the Actions an external Connection also sees as tools named
`<group>__<action>`, listed only where an `autoApproveIf` policy bound to the caller names the
Action: one no policy of the caller's could approve would refuse every call. A call is an ordinary
Action admitted through `submit_decided`, so it either runs and answers as its tool did, or is
refused before anything is persisted, saying why. The listing is presentation only; every call is
decided again at admission.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from fastmcp.server.providers import Provider
from fastmcp.tools import Tool, ToolResult
from fastmcp.utilities.components import FastMCPComponent
from fastmcp.utilities.versions import VersionSpec
from mcp.types import TextContent
from pydantic import ConfigDict, JsonValue

from agentplane.action_service.catalog import DIRECT_TOOL_SEPARATOR, ActionCatalog, ActionIdentity
from agentplane.action_service.models import CallerPrincipal
from agentplane.action_service.service import ActionService

# A direct call waits this long, the most a receipt wait allows; then it answers with the request id.
DIRECT_WAIT_SECONDS = 30
# What an operator reading the history sees for a request no caller wrote a title for.
DIRECT_CALL_TITLE = "Direct tool call"

type DirectCall = Callable[[ActionIdentity, dict[str, JsonValue]], Awaitable[ToolResult]]


class DirectTool(Tool):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    action: ActionIdentity
    call: DirectCall

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        return await self.call(self.action, arguments)


class DirectToolProvider(Provider):
    """The requesting external Connection's direct tools, computed per request."""

    def __init__(
        self,
        catalog: ActionCatalog,
        service: ActionService,
        external_caller: Callable[[], CallerPrincipal | None],
        call: DirectCall,
        wait_seconds: float,
    ) -> None:
        super().__init__()
        self._catalog = catalog
        self._service = service
        self._external_caller = external_caller
        self._call = call
        self._note = (
            "Runs as an Agentplane Action under your policy: a call it does not approve automatically is refused "
            f"without running, and one still running after {wait_seconds:g}s answers with a request id to wait on "
            "with get_action_result."
        )

    async def _list_tools(self) -> Sequence[Tool]:
        principal = self._external_caller()
        if principal is None:
            return []
        approvable = self._service.auto_approvable(principal)
        return [
            self._tool(action)
            for key, group in sorted(self._catalog.groups.items())
            if group.available and group.direct_tools is not None
            for name in sorted(group.direct_tools)
            if name in group.actions and (action := ActionIdentity(group=key, name=name)) in approvable
        ]

    async def _get_tool(self, name: str, version: VersionSpec | None = None) -> Tool | None:
        """Any configured direct tool, approvable or not: a call no policy approves is refused with
        the reason, where an unlisted name would read as a tool that does not exist."""
        if self._external_caller() is None:
            return None
        group_key, separator, action_key = name.partition(DIRECT_TOOL_SEPARATOR)
        group = self._catalog.groups.get(group_key)
        if (
            not separator
            or group is None
            or group.direct_tools is None
            or action_key not in group.direct_tools
            or action_key not in group.actions
        ):
            return None
        return self._tool(ActionIdentity(group=group_key, name=action_key))

    async def get_tasks(self) -> Sequence[FastMCPComponent]:
        # FastMCP collects task components at startup, when no request names a caller to list for.
        return []

    def _tool(self, action: ActionIdentity) -> DirectTool:
        group = self._catalog.groups[action.group]
        definition = group.actions[action.name]
        return DirectTool(
            name=f"{action.group}{DIRECT_TOOL_SEPARATOR}{action.name}",
            title=f"{group.title}: {definition.title}" if definition.title is not None else None,
            description=f"{definition.description}\n\n{self._note}",
            parameters=definition.input_schema or {"type": "object"},
            annotations=definition.annotations,
            # A refusal or an unfinished call answers too, and neither matches the tool's own output.
            output_schema=None,
            action=action,
            call=self._call,
        )


def refusal(action: ActionIdentity, reasons: str) -> ToolResult:
    return ToolResult(
        content=[
            TextContent(
                type="text",
                text=f"Not run, and nothing was submitted: no policy of yours approves this call automatically "
                f"({reasons}). To ask the operator instead, call request_action with action "
                f"{json.dumps(action.model_dump())}, these arguments, a title and a fresh idempotency_key.",
            )
        ],
        is_error=True,
    )
