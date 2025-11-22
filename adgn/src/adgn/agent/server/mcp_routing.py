"""Token-based MCP connection routing.

Routes incoming MCP connections to different backend servers based on Bearer token.
- Human tokens route to agents management server
- Agent tokens route to specific agent's compositor
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import Request, Response
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from adgn.agent.types import AgentID

if TYPE_CHECKING:
    from adgn.agent.runtime.registry import AgentRegistry
    from adgn.mcp.notifying_fastmcp import NotifyingFastMCP

logger = logging.getLogger(__name__)

BEARER_PREFIX = "Bearer "


class TokenRole(StrEnum):
    """MCP connection routing roles."""

    HUMAN = "human"  # Routes to agents management server
    AGENT = "agent"  # Routes to agent's compositor


class HumanTokenInfo(BaseModel):
    """Token info for human connections (routes to agents management server)."""

    role: Literal[TokenRole.HUMAN]

    model_config = ConfigDict(frozen=True)


class AgentTokenInfo(BaseModel):
    """Token info for agent connections (routes to agent's compositor)."""

    role: Literal[TokenRole.AGENT]
    agent_id: AgentID  # Required for agent tokens

    model_config = ConfigDict(frozen=True)


TokenInfo = Annotated[HumanTokenInfo | AgentTokenInfo, Field(discriminator="role")]


class TokenInfoMapping:
    """Maps Bearer tokens to TokenInfo from a JSON file.

    File format:
        {
          "human-token-123": {"role": "human"},
          "agent-token-abc": {"role": "agent", "agent_id": "agent-1"}
        }
    """

    def __init__(self, path: Path):
        self.path = path
        self._mapping: dict[str, TokenInfo] = {}
        self.reload()

    def reload(self) -> None:
        """Reload mapping from file."""
        if not self.path.exists():
            raise FileNotFoundError(f"Token mapping file not found: {self.path}")

        data = json.loads(self.path.read_text())
        if not isinstance(data, dict):
            raise ValueError("Token mapping must be a JSON object")

        # Validate and convert to TokenInfo instances
        mapping: dict[str, TokenInfo] = {}
        adapter = TypeAdapter(TokenInfo)
        for token, token_data in data.items():
            if not isinstance(token, str) or not isinstance(token_data, dict):
                raise ValueError(f"Invalid mapping entry: {token} -> {token_data}")
            mapping[token] = adapter.validate_python(token_data)

        self._mapping = mapping
        logger.info(f"Loaded {len(self._mapping)} token mappings from {self.path}")

    def get(self, token: str) -> TokenInfo | None:
        """Get TokenInfo for a token, or None if not found."""
        return self._mapping.get(token)


class MCPRoutingMiddleware(BaseHTTPMiddleware):
    """Routes MCP connections based on Bearer token to appropriate backend server.

    Token roles:
    - HUMAN: Routes to agents management server (cross-agent operations)
    - AGENT: Routes to specific agent's compositor (agent-specific MCP access)

    Token format: Bearer <token>
    """

    def __init__(
        self, app: ASGIApp, token_mapping: TokenInfoMapping | dict[str, TokenInfo], registry: AgentRegistry, agents_server: NotifyingFastMCP
    ):
        super().__init__(app)
        # Support both TokenInfoMapping and dict for backwards compatibility
        if isinstance(token_mapping, dict):
            # For testing: wrap dict in a simple object with .get() method
            class _DictMapping:
                def __init__(self, d: dict[str, TokenInfo]):
                    self._d = d
                def get(self, token: str) -> TokenInfo | None:
                    return self._d.get(token)
            self.token_mapping = _DictMapping(token_mapping)
        else:
            self.token_mapping = token_mapping
        self.registry = registry
        self.agents_server = agents_server
        # Cache for backend ASGI apps by token info
        self._backend_apps: dict[TokenInfo, ASGIApp] = {}

    def _extract_bearer_token(self, headers: list[tuple[bytes, bytes]]) -> str | None:
        """Extract Bearer token from Authorization header."""
        for name, value in headers:
            if name.lower() == b"authorization":
                auth_value = value.decode("utf-8")
                if auth_value.startswith(BEARER_PREFIX):
                    return auth_value.removeprefix(BEARER_PREFIX)
        return None

    async def _get_backend_app(self, token_info: TokenInfo) -> ASGIApp:
        """Get or create backend ASGI app for the given token info."""
        if token_info not in self._backend_apps:
            match token_info:
                case HumanTokenInfo():
                    # Use the agents management server's HTTP app
                    self._backend_apps[token_info] = self.agents_server.http_app()  # type: ignore[assignment]
                case AgentTokenInfo(agent_id=agent_id):
                    # Get the agent's compositor HTTP app
                    container = await self.registry.ensure_live(agent_id, with_ui=False)
                    compositor_app = container.running.compositor.http_app()
                    self._backend_apps[token_info] = compositor_app  # type: ignore[assignment]
        return self._backend_apps[token_info]

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """Route request to appropriate backend based on token."""
        # Extract Bearer token
        token = self._extract_bearer_token(request.scope["headers"])
        if not token:
            logger.warning("Missing Authorization header")
            return Response(content="Missing Authorization header", status_code=401)

        if not (token_info := self.token_mapping.get(token)):
            logger.warning(f"Invalid token: {token[:10]}...")
            return Response(content="Invalid token", status_code=401)

        logger.info(f"Routing MCP request: token_info={token_info}")

        # Get backend app
        backend_app = await self._get_backend_app(token_info)

        # Forward request to backend ASGI app
        response_started = False
        status_code = 200
        headers = []
        body_parts = []

        async def send(message):
            nonlocal response_started, status_code, headers
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = message.get("headers", [])
            elif message["type"] == "http.response.body":
                body_parts.append(message.get("body", b""))

        # Call backend ASGI app
        await backend_app(request.scope, request.receive, send)

        return Response(
            content=b"".join(body_parts), status_code=status_code, headers={k.decode(): v.decode() for k, v in headers}
        )
