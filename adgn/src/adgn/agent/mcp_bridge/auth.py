"""Authentication middleware for HTTP MCP Bridge.

Reads Bearer token from Authorization header and maps it to agent_id.
Enables multi-tenancy: different tokens → different agent_ids → isolated infrastructure.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import json
import logging
import os
from pathlib import Path
import secrets

from fastapi import HTTPException, Request, Response, status
from starlette.middleware.base import BaseHTTPMiddleware

from adgn.agent.types import AgentID

logger = logging.getLogger(__name__)


def extract_bearer_token(auth_header: str) -> str | None:
    """Extract Bearer token from Authorization header.

    Args:
        auth_header: Authorization header value (e.g., "Bearer abc123")

    Returns:
        Token string if valid Bearer format, None otherwise
    """
    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


class TokenMapping:
    """Maps Bearer tokens to agent_ids from a JSON file.

    File format:
        {
          "secret-token-123": "chatgpt-agent",
          "secret-token-456": "claude-agent"
        }
    """

    def __init__(self, path: Path):
        self.path = path
        self._mapping: dict[str, AgentID] = {}
        self.reload()

    def reload(self) -> None:
        """Reload mapping from file."""
        if not self.path.exists():
            raise FileNotFoundError(f"Token mapping file not found: {self.path}")

        data = json.loads(self.path.read_text())
        if not isinstance(data, dict):
            raise ValueError("Token mapping must be a JSON object")

        # Validate all values are strings and convert to AgentID
        mapping: dict[str, AgentID] = {}
        for token, agent_id in data.items():
            if not isinstance(token, str) or not isinstance(agent_id, str):
                raise ValueError(f"Invalid mapping: {token} -> {agent_id}")
            mapping[token] = AgentID(agent_id)

        self._mapping = mapping
        logger.info(f"Loaded {len(self._mapping)} token mappings from {self.path}")

    def get_agent_id(self, token: str) -> AgentID | None:
        """Get agent_id for a token, or None if not found."""
        return self._mapping.get(token)


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Validates Bearer token and injects agent_id into request state.

    Adds request.state.agent_id for downstream handlers to use.
    Returns 401 if token is missing or invalid.
    """

    def __init__(self, app, token_mapping: TokenMapping):
        super().__init__(app)
        self.token_mapping = token_mapping

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if not (auth_header := request.headers.get("Authorization")):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing Authorization header",
                headers={"WWW-Authenticate": "Bearer"},
            )

        if (token := extract_bearer_token(auth_header)) is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Authorization header format (expected: Bearer <token>)",
                headers={"WWW-Authenticate": "Bearer"},
            )

        if (agent_id := self.token_mapping.get_agent_id(token)) is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token", headers={"WWW-Authenticate": "Bearer"}
            )

        request.state.agent_id = agent_id
        logger.debug(f"Authenticated request: token → {agent_id=}")

        return await call_next(request)


def generate_ui_token() -> str:
    """Generate UI token for Management UI access.

    Reads from ADGN_UI_TOKEN environment variable if set, otherwise generates a random token.
    The random token is 32 bytes (256 bits) encoded as URL-safe base64 (43 characters).

    Returns:
        UI token string for Bearer authentication
    """
    if env_token := os.environ.get("ADGN_UI_TOKEN"):
        logger.info("Using ADGN_UI_TOKEN from environment")
        return env_token

    token = secrets.token_urlsafe(32)
    logger.info("Generated random UI token (set ADGN_UI_TOKEN environment variable to use a fixed token)")
    return token


class UITokenAuthMiddleware:
    """Validates UI token for Management UI access (pure ASGI middleware).

    Simpler than TokenAuthMiddleware - just validates a single token for accessing the management UI.
    No multi-tenancy: all authenticated requests get the same access.

    Implemented as pure ASGI middleware instead of BaseHTTPMiddleware to avoid
    exception handling issues with anyio task groups.
    """

    def __init__(self, app, expected_token: str):
        self.app = app
        self.expected_token = expected_token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Parse headers
        headers = dict(scope.get("headers", []))

        # Validate authentication
        error_response = None
        if not (auth_header := headers.get(b"authorization", b"").decode()):
            error_response = self._create_error_response(
                401, "Missing Authorization header"
            )
        elif (token := extract_bearer_token(auth_header)) is None:
            error_response = self._create_error_response(
                401, "Invalid Authorization header format (expected: Bearer <token>)"
            )
        elif token != self.expected_token:
                error_response = self._create_error_response(
                    401, "Invalid token"
                )

        # Send error or continue
        if error_response:
            await send({
                "type": "http.response.start",
                "status": error_response["status"],
                "headers": error_response["headers"],
            })
            await send({
                "type": "http.response.body",
                "body": error_response["body"],
            })
        else:
            logger.debug("Authenticated UI request")
            await self.app(scope, receive, send)

    def _create_error_response(self, status_code: int, detail: str) -> dict:
        """Create error response dict."""
        body = json.dumps({"detail": detail}).encode()
        return {
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b"Bearer"),
            ],
            "body": body,
        }
