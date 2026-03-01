"""pytest configuration for approval_gate tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from fastmcp.client import Client
from mcp import types as mcp_types

from approval_gate.models import Action, ActionKey
from approval_gate.storage import ActionStorage


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio to auto mode with function-scoped event loops."""
    config.addinivalue_line("markers", "asyncio: mark test as async")
    config.option.asyncio_mode = "auto"
    # Ensure each test gets its own event loop. The default (None → session scope)
    # causes all tests to share one loop, leading to cross-test contamination when
    # one test's background tasks or anyio cancel scopes outlive the test.
    # config.override_ini is only available from pytest 9.1+; for 9.0.x we write
    # directly to _inicache, which getini() consults on every subsequent call.
    config._inicache["asyncio_default_fixture_loop_scope"] = "function"


class GateClient(Client):
    """MCP Client subclass with typed methods for approval gate tools."""

    async def call_gate_tool(self, tool_name: str, args: dict[str, object]) -> ActionKey:
        """Call a gate-wrapped tool and parse the ActionKey from the result."""
        result = await self.call_tool_mcp(tool_name, args)
        item = result.content[0]
        assert isinstance(item, mcp_types.TextContent)
        return ActionKey.model_validate_json(item.text)

    async def call_echo(self, text: str, *, justification: str = "test", session_key: str) -> ActionKey:
        return await self.call_gate_tool(
            "test_echo", {"input": {"text": text}, "justification": justification, "session_key": session_key}
        )

    async def approve(self, key: ActionKey) -> Action:
        return await self._call_operator_tool("approve_action", {"key": key.model_dump()})

    async def reject(self, key: ActionKey, reason: str | None = None) -> Action:
        return await self._call_operator_tool("reject_action", {"key": key.model_dump(), "reason": reason})

    async def _call_operator_tool(self, name: str, args: dict[str, object]) -> Action:
        result = await self.call_tool_mcp(name, args)
        item = result.content[0]
        assert isinstance(item, mcp_types.TextContent)
        return Action.model_validate_json(item.text)


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncGenerator[ActionStorage]:
    """Temporary in-memory storage for tests."""
    store = await ActionStorage.initialize(tmp_path / "test.db")
    try:
        yield store
    finally:
        await store.close()
