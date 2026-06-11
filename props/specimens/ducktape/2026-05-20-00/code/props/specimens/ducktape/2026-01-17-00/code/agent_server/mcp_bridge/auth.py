"""Token authentication and routing for MCP bridge.

Provides:
- Token loading from YAML config via Pydantic model
- ASGI-level routing based on bearer token
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

if TYPE_CHECKING:
    from agent_server.agent_types import AgentID

logger = logging.getLogger(__name__)

# Default tokens config path
DEFAULT_TOKENS_PATH = Path("~/.config/adgn/tokens.yaml").expanduser()


class TokensConfig(BaseModel):
    """Tokens configuration loaded from YAML.

    File format:
    ```yaml
    users:
      admin: "hex_token_here"

    agents:
      claude-code-1: "hex_token_here"
    ```

    Null or empty token values are skipped when generating inverted token mappings.
    """

    users: dict[str, str | None] = {}  # user_id -> token (None allowed for disabled users)
    agents: dict[str, str | None] = {}  # agent_id -> token (None allowed for disabled agents)

    @classmethod
    def from_yaml_file(cls, path: Path | None = None) -> TokensConfig:
        """Load tokens config from YAML file.

        Args:
            path: Path to tokens.yaml. Defaults to ~/.config/adgn/tokens.yaml
                  or ADGN_TOKENS_PATH env var.

        Returns:
            TokensConfig instance (empty if file doesn't exist)
        """
        config_path = path or Path(os.getenv("ADGN_TOKENS_PATH", str(DEFAULT_TOKENS_PATH)))

        if not config_path.exists():
            logger.warning(f"Tokens config not found at {config_path}, using empty tokens")
            return cls()

        with config_path.open() as f:
            data = yaml.safe_load(f) or {}

        config = cls.model_validate(data)
        logger.info(f"Loaded {len(config.users)} user tokens, {len(config.agents)} agent tokens")
        return config

    @staticmethod
    def _invert_tokens(mapping: dict[str, str | None]) -> dict[str, str]:
        """Invert id->token mapping to token->id, filtering None values."""
        return {token: id_ for id_, token in mapping.items() if token}

    def user_tokens(self) -> dict[str, str]:
        """Return token -> user_id mapping (inverted from config)."""
        return self._invert_tokens(self.users)

    def agent_tokens(self) -> dict[str, str]:
        """Return token -> agent_id mapping (inverted from config)."""
        return self._invert_tokens(self.agents)


class TokenRoutingASGI:
    """ASGI app that routes /mcp requests to different MCP servers based on bearer token.

    This is NOT middleware - it's a top-level ASGI app that dispatches to
    completely different ASGI applications based on the token.

    Token routing:
    - User tokens → global user-facing compositor (sees all agents)
    - Agent tokens → that agent's agent-facing compositor (with policy gateway)
    - No token or invalid token → 401 Unauthorized
    """

    def __init__(
        self,
        user_tokens: dict[str, str],  # token → user_id
        agent_tokens: dict[str, AgentID],  # token → agent_id
        user_app: ASGIApp,  # ASGI app for user compositor
        agent_apps: dict[AgentID, ASGIApp],  # ASGI apps for agent compositors
    ):
        self.user_tokens = user_tokens
        self.agent_tokens = agent_tokens
        self.user_app = user_app
        self.agent_apps = agent_apps

    @staticmethod
    async def _send_error(scope: Scope, receive: Receive, send: Send, message: str, status_code: int) -> None:
        """Send an HTTP error response."""
        await Response(message, status_code=status_code)(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Passthrough for lifespan, websocket, etc.
            await self.user_app(scope, receive, send)
            return

        # Extract Authorization header
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode()

        if not auth.startswith("Bearer "):
            await self._send_error(scope, receive, send, "Unauthorized: Bearer token required", 401)
            return

        token = auth[7:]

        # Route based on token
        if user_id := self.user_tokens.get(token):
            logger.debug(f"Routing to user compositor for user: {user_id}")
            await self.user_app(scope, receive, send)
        elif agent_id := self.agent_tokens.get(token):
            if agent_app := self.agent_apps.get(agent_id):
                logger.debug(f"Routing to agent compositor for agent: {agent_id}")
                await agent_app(scope, receive, send)
            else:
                logger.warning(f"Agent app not found for agent_id: {agent_id}")
                await self._send_error(scope, receive, send, f"Agent not found: {agent_id}", 404)
        else:
            await self._send_error(scope, receive, send, "Invalid token", 401)
