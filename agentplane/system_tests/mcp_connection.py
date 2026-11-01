"""An external MCP client using real registration, operator consent and Dex authorization."""

import asyncio
import logging
import ssl
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx
import httpx2
from fastmcp.client.auth.oauth import TokenStorageAdapter
from google.protobuf.json_format import MessageToDict, ParseDict
from key_value.aio.stores.memory import MemoryStore
from mcp.client.auth.oauth2 import OAuthClientProvider
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata
from pydantic import AnyUrl

from agentplane.mcp_edge import connection_service_pb2
from agentplane.proto import sandboxes_pb2
from agentplane.system_tests.operator_login import LoginBlockedError, OperatorCredentials, follow_dex_authorization

REDIRECT_URI = "http://127.0.0.1:49152/test-mcp-callback"


class ExternalMCPSession:
    """Expose tool requests without allowing SDK transport details into test failures."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def list_tools(self) -> Any:
        __tracebackhide__ = True
        try:
            return await self._session.list_tools()
        except Exception as error:
            error_type = type(error).__name__
            del error
            raise LoginBlockedError(f"External MCP tool request failed: {error_type}; details withheld") from None

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        __tracebackhide__ = True
        try:
            return await self._session.call_tool(name, arguments=arguments)
        except Exception as error:
            error_type = type(error).__name__
            del error
            raise LoginBlockedError(f"External MCP tool request failed: {error_type}; details withheld") from None


def _origin(url: httpx.URL) -> httpx.URL:
    return url.copy_with(path="/", query=None, fragment=None)


def _require(condition: bool, message: str) -> None:
    __tracebackhide__ = True
    if not condition:
        raise LoginBlockedError(message)


def _query(url: httpx.URL, name: str) -> str:
    __tracebackhide__ = True
    values = url.params.get_list(name)
    _require(len(values) == 1 and bool(values[0]), "MCP authorization omitted a required redirect parameter")
    return values[0]


async def _authorize(
    url: str,
    *,
    server: str,
    operator: httpx.AsyncClient,
    credentials: OperatorCredentials,
    account: sandboxes_pb2.ServiceAccount,
    browser: httpx.AsyncClient,
    dex_browser: httpx.AsyncClient,
) -> AuthorizationCodeResult:
    __tracebackhide__ = True
    authorization, edge = httpx.URL(url), _origin(httpx.URL(server))
    _require(
        _origin(authorization) == edge and authorization.path == "/authorize",
        "MCP authorization did not use the configured edge",
    )
    expected_state = _query(authorization, "state")
    enrolled = await browser.get(authorization)
    _require(enrolled.status_code == 302, "MCP authorization did not redirect to consent")
    consent = enrolled.url.join(enrolled.headers["location"])
    _require(
        _origin(consent) == _origin(operator.base_url) and consent.fragment.startswith("/connection-enrollments/"),
        "MCP consent did not use the configured operator app",
    )
    handle = consent.fragment.removeprefix("/connection-enrollments/")
    _require(bool(handle) and "/" not in handle, "MCP consent omitted its enrollment handle")
    route = f"/upstream/connections/v1/operator/connection-enrollments/{quote(handle, safe='')}"
    response = await operator.post(route + "/preview", json={"handle": handle})
    _require(response.status_code == 200, "MCP consent preview was refused")
    preview = ParseDict(response.json(), connection_service_pb2.PreviewConnectionEnrollmentResponse())
    decision = connection_service_pb2.DecideConnectionEnrollmentRequest(
        handle=handle,
        expected_version=preview.version,
        allow=connection_service_pb2.EnrollmentAllow(
            service_account=account,
            new_connection=connection_service_pb2.NewConnection(display_name="Test external Session coordinator"),
        ),
    )
    response = await operator.post(route + "/decision", json=MessageToDict(decision))
    _require(response.status_code == 200, "MCP consent decision was refused")
    decided = ParseDict(response.json(), connection_service_pb2.DecideConnectionEnrollmentResponse())
    _require(decided.HasField("redirect_url"), "MCP consent did not return an authorization URL")
    state, code = await follow_dex_authorization(
        decided.redirect_url, dex_browser, credentials, callback_origin=edge, callback_path="/auth/callback"
    )
    callback = await browser.get(edge.join("/auth/callback").copy_merge_params({"state": state, "code": code}))
    _require(callback.is_redirect, "MCP upstream callback did not redirect to the client")
    redirect = callback.url.join(callback.headers["location"])
    _require(
        redirect.copy_with(query=None) == httpx.URL(REDIRECT_URI),
        "MCP upstream callback changed the registered client redirect",
    )
    _require(_query(redirect, "state") == expected_state, "MCP authorization changed client state")
    return AuthorizationCodeResult(code=_query(redirect, "code"), state=expected_state, iss=_query(redirect, "iss"))


@asynccontextmanager
async def connected_session(
    *,
    server: str,
    operator: httpx.AsyncClient,
    credentials: OperatorCredentials,
    account: sandboxes_pb2.ServiceAccount,
    ca_bundle: Path,
) -> AsyncIterator[ExternalMCPSession]:
    """Complete a fresh Connection grant, then yield its authenticated SDK Session.

    Operator consent is setup. Subsequent tool calls use only the external client's OAuth grant,
    without the app session, a workload token, Kubernetes credentials or an execution environment.
    """
    __tracebackhide__ = True
    storage = TokenStorageAdapter(MemoryStore(), server_url=server)
    metadata = OAuthClientMetadata(
        client_name=f"Test external Session coordinator {uuid4()}",
        redirect_uris=[AnyUrl(REDIRECT_URI)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )
    result: AuthorizationCodeResult | None = None
    trust = ssl.create_default_context(cafile=ca_bundle)
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        async with AsyncExitStack() as resources:
            browser = await resources.enter_async_context(
                httpx.AsyncClient(verify=trust, timeout=15, follow_redirects=False)
            )
            dex_browser = await resources.enter_async_context(
                httpx.AsyncClient(verify=trust, timeout=15, follow_redirects=False)
            )

            async def redirect(url: str) -> None:
                __tracebackhide__ = True
                nonlocal result
                result = await _authorize(
                    url,
                    server=server,
                    operator=operator,
                    credentials=credentials,
                    account=account,
                    browser=browser,
                    dex_browser=dex_browser,
                )

            async def callback() -> AuthorizationCodeResult:
                __tracebackhide__ = True
                _require(result is not None, "MCP client requested a callback before authorization")
                assert result is not None
                return result

            provider = OAuthClientProvider(
                server_url=server,
                client_metadata=metadata,
                storage=storage,
                redirect_handler=redirect,
                callback_handler=callback,
            )
            try:
                async with asyncio.timeout(60):
                    http = await resources.enter_async_context(
                        httpx2.AsyncClient(auth=provider, verify=trust, timeout=15, follow_redirects=False)
                    )
                    read, write = await resources.enter_async_context(streamable_http_client(server, http_client=http))
                    session = await resources.enter_async_context(ClientSession(read, write))
                    await session.initialize()
            except Exception as error:
                error_type = type(error).__name__
                del error
                raise LoginBlockedError(
                    f"External MCP authorization failed: {error_type}; OAuth details withheld"
                ) from None
            yield ExternalMCPSession(session)
    finally:
        await storage.clear()
        logging.disable(previous_logging)
