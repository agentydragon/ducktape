"""Haku-specific OAuth consent and agent naming.

The generic Authentik infrastructure owns OAuth reliability and token validation. This module owns
Haku's product ceremony: asking the operator for a unique agent name and carrying it to the verified
operator-binding hook without replacing FastMCP's transaction, CSRF, redirect, or PKCE machinery.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastmcp.server.auth.oauth_proxy.models import ProxyDCRClient
from jinja2 import Environment, FileSystemLoader, select_autoescape
from key_value.aio.adapters.pydantic import PydanticAdapter
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from mcp_infra.authentik_auth.auth import ResilientOIDCProxy

if TYPE_CHECKING:
    from mcp.server.auth.provider import AuthorizationCode
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

MAX_AGENT_DISPLAY_NAME_LENGTH = 80
OnAgentAuthorized = Callable[[str, Mapping[str, Any], str], Awaitable[None]]

_TEMPLATES = Environment(
    loader=FileSystemLoader(Path(__file__).with_name("templates")),
    autoescape=select_autoescape(enabled_extensions=("html", "j2"), default_for_string=True),
)


class PendingAgentName(BaseModel):
    client_id: str
    display_name: str
    created_at: float


def normalize_agent_display_name(value: str) -> str:
    display_name = " ".join(value.split())
    if not display_name:
        raise ValueError("Enter a name for this agent.")
    if len(display_name) > MAX_AGENT_DISPLAY_NAME_LENGTH:
        raise ValueError(f"Agent names must be at most {MAX_AGENT_DISPLAY_NAME_LENGTH} characters.")
    return display_name


class AgentNamingOIDCProxy(ResilientOIDCProxy):
    """FastMCP OAuth proxy with Haku's required agent-name ceremony."""

    def __init__(self, *args: Any, on_agent_authorized: OnAgentAuthorized, **kwargs: Any) -> None:
        if kwargs.get("on_client_authorized") is not None:
            raise ValueError("AgentNamingOIDCProxy owns the authorization hook")
        kwargs["on_client_authorized"] = None
        super().__init__(*args, **kwargs)
        self._on_agent_authorized = on_agent_authorized
        self._pending_agent_name_store = PydanticAdapter[PendingAgentName](
            key_value=self._client_storage,
            pydantic_model=PendingAgentName,
            default_collection="mcp-pending-agent-names",
            raise_on_validation_error=True,
        )

    def _decode_list_cookie(self, request: Request, base_name: str) -> list[str]:
        """Always show Haku's naming ceremony; retain FastMCP's browser-binding state."""
        if base_name in {"MCP_APPROVED_CLIENTS", "MCP_DENIED_CLIENTS"}:
            return []
        return super()._decode_list_cookie(request, base_name)

    async def _render_consent(self, transaction: Any, *, agent_name: str = "", error: str | None = None) -> str:
        client = await self.get_client(transaction.client_id)
        return _TEMPLATES.get_template("mcp_agent_consent.html.j2").render(
            client_id=transaction.client_id,
            client_name=client.client_name if isinstance(client, ProxyDCRClient) else None,
            redirect_uri=transaction.client_redirect_uri,
            scopes=transaction.scopes,
            txn_id=transaction.txn_id,
            csrf_token=transaction.csrf_token,
            agent_name=agent_name,
            error=error,
            max_agent_display_name_length=MAX_AGENT_DISPLAY_NAME_LENGTH,
        )

    async def _show_consent_page(self, request: Request) -> HTMLResponse | RedirectResponse:
        response = await super()._show_consent_page(request)
        if not isinstance(response, HTMLResponse) or response.status_code != 200:
            return response

        txn_id = request.query_params.get("txn_id")
        transaction = await self._transaction_store.get(key=txn_id) if txn_id else None
        if transaction is None or transaction.csrf_token is None:
            return response
        response.body = (await self._render_consent(transaction)).encode()
        response.headers["content-length"] = str(len(response.body))
        return response

    async def _submit_consent(self, request: Request) -> RedirectResponse | HTMLResponse:
        form = await request.form()
        action = str(form.get("action", ""))
        txn_id = str(form.get("txn_id", ""))
        transaction = await self._transaction_store.get(key=txn_id) if txn_id else None

        display_name: str | None = None
        if action == "approve":
            submitted_name = str(form.get("agent_name", ""))
            try:
                display_name = normalize_agent_display_name(submitted_name)
            except ValueError as error:
                if transaction is None or transaction.csrf_token is None:
                    return HTMLResponse("Invalid OAuth transaction", status_code=400)
                return HTMLResponse(
                    await self._render_consent(transaction, agent_name=submitted_name, error=str(error)),
                    status_code=400,
                )

        response = await super()._submit_consent(request)
        if display_name is not None and transaction is not None and isinstance(response, RedirectResponse):
            await self._pending_agent_name_store.put(
                key=transaction.client_id,
                value=PendingAgentName(
                    client_id=transaction.client_id, display_name=display_name, created_at=time.time()
                ),
                ttl=15 * 60,
            )
        return response

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if not client.client_id:
            raise RuntimeError("authorized MCP client is missing its DCR identity")
        code_model = await self._code_store.get(key=authorization_code.code)
        pending_name = await self._pending_agent_name_store.get(key=client.client_id)
        if code_model is None or pending_name is None:
            raise RuntimeError("authorized MCP client is missing its upstream identity or required agent name")
        await self._on_agent_authorized(client.client_id, code_model.idp_tokens, pending_name.display_name)
        await self._pending_agent_name_store.delete(key=client.client_id)
        return await super().exchange_authorization_code(client, authorization_code)
