"""Matrix control MCP: yield-only control, no network I/O.

Purpose
- Provide a small control-plane for Matrix-driven agents that do I/O via
  docker_exec + CLI tools. The agent can:
  - get_state() → obtain current sync cursor (since) and last seen event id
  - yield(since, last_event_id?) → update cursor and end the turn via UiBus

No Matrix network calls occur inside this server — it is transport-agnostic and
purely local state + UI bus signaling.
"""

from __future__ import annotations

from agent_server.server.bus import ServerBus, UiEndTurn
from mcp_infra.enhanced.server import EnhancedFastMCP


def make_matrix_control_server(bus: ServerBus) -> EnhancedFastMCP:
    mcp = EnhancedFastMCP(
        "Matrix Control Server", instructions=("Matrix control: yield-only control to signal end of turn.")
    )

    @mcp.flat_model()
    def do_yield() -> UiEndTurn:
        """End the current turn. The runner will wake you on new DMs."""
        bus.push_end_turn()
        return UiEndTurn()

    return mcp
