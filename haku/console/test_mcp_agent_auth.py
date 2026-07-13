"""Haku-specific OAuth agent naming tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest_bazel
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from starlette.responses import HTMLResponse, RedirectResponse

from haku.console.mcp_agent_auth import AgentNamingOIDCProxy
from mcp_infra.authentik_auth.auth import ResilientOIDCProxy


async def test_consent_replaces_only_successful_fastmcp_page() -> None:
    proxy = AgentNamingOIDCProxy.__new__(AgentNamingOIDCProxy)
    transaction = SimpleNamespace(
        client_id="dcr-xyz",
        client_redirect_uri="https://claude.ai/api/mcp/auth_callback",
        scopes=["openid", "profile"],
        txn_id="txn-1",
        csrf_token="csrf-1",
    )
    proxy._transaction_store = AsyncMock()
    proxy._transaction_store.get = AsyncMock(return_value=transaction)
    request = SimpleNamespace(query_params={"txn_id": "txn-1"})
    stock_page = HTMLResponse("FastMCP default")

    with (
        patch.object(OAuthProxy, "_show_consent_page", AsyncMock(return_value=stock_page)),
        patch.object(AgentNamingOIDCProxy, "get_client", AsyncMock(return_value=None)),
    ):
        response = await AgentNamingOIDCProxy._show_consent_page(proxy, cast(Any, request))

    assert response.status_code == 200
    body = bytes(response.body).decode()
    assert "Name this agent" in body
    assert 'name="agent_name"' in body
    assert "FastMCP default" not in body
    assert "dcr-xyz" in body


async def test_template_autoescapes_oauth_and_form_values() -> None:
    proxy = AgentNamingOIDCProxy.__new__(AgentNamingOIDCProxy)
    transaction = SimpleNamespace(
        client_id="<script>alert(1)</script>",
        client_redirect_uri='https://example.test/callback?next="bad"',
        scopes=["openid", "<scope>"],
        txn_id="txn-1",
        csrf_token="csrf-1",
    )
    with patch.object(AgentNamingOIDCProxy, "get_client", AsyncMock(return_value=None)):
        body = await proxy._render_consent(transaction, agent_name='"><script>alert(2)</script>')

    assert "<script>alert" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "&#34;&gt;&lt;script&gt;alert(2)&lt;/script&gt;" in body
    assert "&lt;scope&gt;" in body


def test_naming_does_not_reuse_remembered_approve_or_deny() -> None:
    proxy = AgentNamingOIDCProxy.__new__(AgentNamingOIDCProxy)
    request = cast(Any, object())

    assert proxy._decode_list_cookie(request, "MCP_APPROVED_CLIENTS") == []
    assert proxy._decode_list_cookie(request, "MCP_DENIED_CLIENTS") == []

    with patch.object(OAuthProxy, "_decode_list_cookie", return_value=["csrf-token"]) as inherited:
        assert proxy._decode_list_cookie(request, "MCP_CONSENT_STATE") == ["csrf-token"]
    inherited.assert_called_once_with(request, "MCP_CONSENT_STATE")


async def test_name_is_stored_only_after_fastmcp_accepts_approval() -> None:
    proxy = AgentNamingOIDCProxy.__new__(AgentNamingOIDCProxy)
    proxy._transaction_store = AsyncMock()
    proxy._transaction_store.get = AsyncMock(return_value=SimpleNamespace(client_id="dcr-xyz"))
    proxy._pending_agent_name_store = AsyncMock()
    request = SimpleNamespace(
        form=AsyncMock(
            return_value={
                "txn_id": "txn-1",
                "csrf_token": "csrf-1",
                "action": "approve",
                "agent_name": "  Kitchen   Claude  ",
            }
        )
    )
    accepted = RedirectResponse("https://auth.example.com/authorize", status_code=302)

    with patch.object(OAuthProxy, "_submit_consent", AsyncMock(return_value=accepted)):
        response = await AgentNamingOIDCProxy._submit_consent(proxy, cast(Any, request))

    assert response is accepted
    stored = proxy._pending_agent_name_store.put.await_args.kwargs["value"]
    assert stored.client_id == "dcr-xyz"
    assert stored.display_name == "Kitchen Claude"


async def test_name_is_not_stored_when_fastmcp_rejects_consent() -> None:
    proxy = AgentNamingOIDCProxy.__new__(AgentNamingOIDCProxy)
    proxy._transaction_store = AsyncMock()
    proxy._transaction_store.get = AsyncMock(return_value=SimpleNamespace(client_id="dcr-xyz"))
    proxy._pending_agent_name_store = AsyncMock()
    request = SimpleNamespace(
        form=AsyncMock(
            return_value={
                "txn_id": "txn-1",
                "csrf_token": "forged",
                "action": "approve",
                "agent_name": "Kitchen Claude",
            }
        )
    )
    rejected = HTMLResponse("bad csrf", status_code=403)

    with patch.object(OAuthProxy, "_submit_consent", AsyncMock(return_value=rejected)):
        response = await AgentNamingOIDCProxy._submit_consent(proxy, cast(Any, request))

    assert response is rejected
    proxy._pending_agent_name_store.put.assert_not_awaited()


async def test_authorized_hook_requires_and_consumes_pending_name() -> None:
    proxy = AgentNamingOIDCProxy.__new__(AgentNamingOIDCProxy)
    proxy._on_client_authorized = None
    proxy._on_agent_authorized = AsyncMock()
    idp_tokens = {"id_token": "jwt-value", "access_token": "at"}
    proxy._code_store = AsyncMock()
    proxy._code_store.get = AsyncMock(return_value=SimpleNamespace(idp_tokens=idp_tokens))
    proxy._pending_agent_name_store = AsyncMock()
    proxy._pending_agent_name_store.get = AsyncMock(return_value=SimpleNamespace(display_name="Kitchen Claude"))
    client = SimpleNamespace(client_id="dcr-xyz")
    auth_code = SimpleNamespace(code="the-code")

    with patch.object(ResilientOIDCProxy, "exchange_authorization_code", AsyncMock(return_value=object())):
        await AgentNamingOIDCProxy.exchange_authorization_code(proxy, cast(Any, client), cast(Any, auth_code))

    proxy._on_agent_authorized.assert_awaited_once_with("dcr-xyz", idp_tokens, "Kitchen Claude")
    proxy._pending_agent_name_store.delete.assert_awaited_once_with(key="dcr-xyz")


if __name__ == "__main__":
    pytest_bazel.main()
