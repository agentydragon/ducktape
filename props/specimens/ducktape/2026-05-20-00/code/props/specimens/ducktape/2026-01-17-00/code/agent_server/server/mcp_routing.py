"""MCP routing middleware for FastAPI.

This module provides token-based routing for MCP requests, routing:
- User tokens → global compositor (sees all agents)
- Agent tokens → that agent's compositor (with policy gateway)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from agent_server.mcp_bridge.auth import TokensConfig

if TYPE_CHECKING:
    from fastapi import FastAPI

    from agent_server.runtime.registry import AgentRegistry
    from mcp_infra.compositor.server import Compositor

logger = logging.getLogger(__name__)


# Load token table at module load (can be refreshed via TokensConfig.from_yaml_file)
def _build_token_table() -> dict[str, str]:
    """Build token table mapping token -> identity (user or agent:id)."""
    config = TokensConfig.from_yaml_file()
    table: dict[str, str] = {}

    # User tokens map to "user"
    for token in config.user_tokens():
        table[token] = "user"

    # Agent tokens: token -> agent_id, so build table token -> "agent:{id}"
    for token, agent_id in config.agent_tokens().items():
        table[token] = f"agent:{agent_id}"

    return table


TOKEN_TABLE: dict[str, str] = _build_token_table()


class MCPRoutingMiddleware(BaseHTTPMiddleware):
    """Middleware that routes MCP requests based on bearer token.

    Routes:
    - User tokens → agents_server (global compositor)
    - Agent tokens → that agent's compositor

    Args:
        app: FastAPI application
        token_table: Mapping of token -> identity
        registry: AgentRegistry for looking up agent containers
        agents_server: Global compositor for user requests
    """

    def __init__(
        self, app: FastAPI, *, token_table: dict[str, str], registry: AgentRegistry, agents_server: Compositor
    ) -> None:
        super().__init__(app)
        self.token_table = token_table
        self.registry = registry
        self.agents_server = agents_server

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Route request based on bearer token."""
        # Extract bearer token
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return Response(content="Missing or invalid Authorization header", status_code=401)

        token = auth_header[7:]  # Remove "Bearer " prefix

        # Look up identity
        identity = self.token_table.get(token)
        if identity is None:
            return Response(content="Invalid token", status_code=401)

        # Store identity in request state for downstream handlers
        request.state.mcp_identity = identity

        # Route based on identity
        if identity == "user":
            # User requests go to global compositor
            request.state.mcp_target = self.agents_server
        elif identity.startswith("agent:"):
            # Agent requests go to their compositor
            agent_id = identity[6:]  # Remove "agent:" prefix
            container = self.registry.get(agent_id)
            if container is None:
                return Response(content=f"Agent not found: {agent_id}", status_code=404)
            if container._compositor is None:
                return Response(content=f"Agent compositor not ready: {agent_id}", status_code=503)
            request.state.mcp_target = container._compositor
        else:
            return Response(content=f"Unknown identity type: {identity}", status_code=400)

        return await call_next(request)
