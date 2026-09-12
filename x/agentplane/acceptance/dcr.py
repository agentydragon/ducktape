"""Real MCP SDK discovery/DCR, deliberately stopped before browser authorization.

HTTP event hooks check the wire contract; they never replace a transport or perform
OAuth themselves. Credential-bearing SDK failures terminate at a constant-message
boundary. No response bodies, callback queries, or client credentials are reported.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from uuid import uuid4

import httpx
from fastmcp.client.auth.oauth import TokenStorageAdapter
from key_value.aio.stores.memory import MemoryStore
from mcp.client.auth.oauth2 import OAuthClientProvider
from mcp.client.auth.utils import extract_resource_metadata_from_www_auth
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthMetadata, ProtectedResourceMetadata
from pydantic import AnyUrl


class DcrError(Exception):
    """Safe to render without exposing an SDK exception or HTTP payload."""


class _RegistrationCompleteError(Exception):
    """Stop at the SDK's redirect callback: registration is not completed OAuth."""


def _require(condition: bool, message: str) -> None:
    __tracebackhide__ = True
    if not condition:
        raise DcrError(message)


def _safe_reason(error: BaseException) -> str:
    # Only our own constant-message assertions are printable. SDK/HTTP/Pydantic
    # exceptions can embed credentials, including through ExceptionGroup children.
    if isinstance(error, DcrError):
        return str(error)
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(sorted({_safe_reason(child) for child in error.exceptions}))
    return type(error).__name__


@dataclass(repr=False)
class Registration:
    client: OAuthClientInformationFull = field(repr=False)
    authorization_url: str = field(repr=False)


@dataclass
class _Wire:
    server: str
    stage: str = "MCP challenge"
    resource_metadata_url: str | None = None
    resource: ProtectedResourceMetadata | None = field(default=None, repr=False)
    metadata: OAuthMetadata | None = field(default=None, repr=False)
    registrations: int = 0
    last_status: int | None = None

    async def request(self, request: httpx.Request) -> None:
        __tracebackhide__ = True
        # This deployment intentionally owns its own AS. Fail before sending a
        # registration payload to another origin, even if discovery advertises it.
        server = httpx.URL(self.server)
        _require(
            request.url.copy_with(path="/", query=None, fragment=None)
            == server.copy_with(path="/", query=None, fragment=None),
            "Discovery attempted a request outside the Actions origin",
        )
        _require(not request.url.userinfo and not request.url.query, "Unexpected credential-bearing request URL")
        if request.method == "POST" and request.url != server:
            self.stage = "DCR request"
            _require(self.resource is not None and self.metadata is not None, "DCR attempted without discovery")
            assert self.metadata is not None
            _require(str(request.url) == str(self.metadata.registration_endpoint), "DCR ignored advertised endpoint")
            _require(request.headers.get("content-type", "").split(";")[0] == "application/json", "DCR must send JSON")
            self.registrations += 1
            _require(self.registrations == 1, "Unexpected duplicate registration POST")

    async def response(self, response: httpx.Response) -> None:
        __tracebackhide__ = True
        self.last_status = response.status_code
        if str(response.request.url) == self.server:
            _require(response.status_code == 401, "MCP must challenge an unauthenticated client")
            self.resource_metadata_url = extract_resource_metadata_from_www_auth(response)
            _require(self.resource_metadata_url is not None, "MCP 401 omitted resource_metadata challenge")
            self.stage = "protected-resource metadata"
            return
        _require(
            response.headers.get("content-type", "").split(";")[0] == "application/json",
            "Discovery/DCR response is not application/json",
        )
        await response.aread()
        if str(response.request.url) == self.resource_metadata_url:
            _require(response.status_code == 200, "Advertised protected-resource metadata is unavailable")
            self.resource = ProtectedResourceMetadata.model_validate_json(response.content)
            _require(str(self.resource.resource) == self.server, "Protected-resource identity mismatch")
            _require(bool(self.resource.authorization_servers), "No authorization server advertised")
            self.stage = "authorization-server metadata"
        elif response.request.method == "GET":
            _require(response.status_code == 200, "Authorization-server metadata is unavailable")
            _require(self.resource is not None, "Authorization-server discovery preceded resource discovery")
            assert self.resource is not None
            self.metadata = OAuthMetadata.model_validate_json(response.content)
            _require(
                self.metadata.issuer in self.resource.authorization_servers, "Discovered issuer was not advertised"
            )
            _require(self.metadata.registration_endpoint is not None, "Authorization server does not advertise DCR")
            _require(
                "S256" in (self.metadata.code_challenge_methods_supported or []), "AS does not advertise S256 PKCE"
            )
        else:
            _require(response.status_code == 201, "RFC 7591 registration did not return 201")
            self.stage = "registration validation"


async def register_client(server: str, redirect_uri: str) -> Registration:
    """Initiate MCP using a fresh SDK client; stop only after validated DCR.

    No static client ID, bearer, cached credentials, mock HTTP, browser, or token
    exchange. The returned authorization URL stays in memory for optional testing
    against the real consent boundary. The caller must refuse pytest --showlocals.
    """
    __tracebackhide__ = True
    wire = _Wire(server)
    storage = TokenStorageAdapter(MemoryStore(), server_url=server)
    metadata = OAuthClientMetadata(
        client_name=f"Agentplane DCR acceptance {uuid4()}",
        redirect_uris=[AnyUrl(redirect_uri)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )
    registration: Registration | None = None

    async def redirect(url: str) -> None:
        __tracebackhide__ = True
        nonlocal registration
        client = await storage.get_client_info()
        _require(client is not None, "SDK did not persist registration result")
        assert client is not None
        _require(bool(client.client_id), "Registration omitted client_id")
        _require(client.redirect_uris == metadata.redirect_uris, "Registration changed redirect_uris")
        _require(client.client_name == metadata.client_name, "Registration changed client_name")
        _require(client.grant_types == metadata.grant_types, "Registration changed grant_types")
        _require(client.response_types == metadata.response_types, "Registration changed response_types")
        _require(client.token_endpoint_auth_method == "none", "Registration changed public-client authentication")
        _require(client.client_secret is None, "Public-client registration unexpectedly issued a secret")
        _require(client.scope == metadata.scope, "Registration changed discovered scopes")
        _require(wire.registrations == 1, "SDK did not complete exactly one real registration")
        authorization = httpx.URL(url)
        assert wire.metadata is not None
        _require(
            str(authorization.copy_with(query=None)) == str(wire.metadata.authorization_endpoint),
            "SDK did not use advertised authorization endpoint",
        )
        _require(authorization.params.get("code_challenge_method") == "S256", "SDK did not prepare S256 PKCE")
        _require(bool(authorization.params.get("state")), "SDK did not prepare OAuth state")
        registration = Registration(client=client, authorization_url=url)
        raise _RegistrationCompleteError

    async def callback() -> tuple[str, str | None]:
        raise DcrError("Registration-only probe unexpectedly requested an authorization code")

    auth = OAuthClientProvider(
        server_url=server,
        client_metadata=metadata,
        storage=storage,
        redirect_handler=redirect,
        callback_handler=callback,
        timeout=30,
    )
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        async with asyncio.timeout(60):
            try:
                async with (
                    httpx.AsyncClient(
                        auth=auth,
                        timeout=15,
                        follow_redirects=False,
                        event_hooks={"request": [wire.request], "response": [wire.response]},
                    ) as http,
                    streamable_http_client(server, http_client=http) as (read, write, _),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
            except* _RegistrationCompleteError:
                pass
            _require(registration is not None, "MCP initialization ended without validated registration")
            _require(await storage.get_tokens() is None, "Registration-only probe obtained a token")
            assert registration is not None
            return registration
    except Exception as error:
        # SDK errors may include response bodies or OAuth state. Do not chain them,
        # including when nested inside the transport's AnyIO exception groups.
        raise DcrError(
            f"DCR acceptance failed at {wire.stage} (HTTP {wire.last_status}): "
            f"{_safe_reason(error)}; HTTP/OAuth details withheld"
        ) from None
    finally:
        await storage.clear()
        logging.disable(previous_logging)
