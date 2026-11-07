"""One gateway binding across synthetic control and private HTTP services."""

from __future__ import annotations

import asyncio
import ssl
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import override

import grpc
import httpx
import httpx2
import pytest
import pytest_bazel
from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import BaseModel, SecretStr

from platform_sessions.auth import Bearer, BearerAuth
from platform_sessions.host import create_shell
from platform_sessions.provider.binding import RemoteProviderSpec, bind_remote_provider, open_remote_provider
from platform_sessions.provider.v1 import session_provider_pb2 as pb, session_provider_pb2_grpc as rpc
from platform_sessions.provider_proxy import ProviderProxy
from platform_sessions.proxy import ProxyInstance, ProxyRequest, ProxyResponse, install_proxy
from platform_sessions.registry import IntegrationInstance
from platform_sessions.remote_transport import RemoteSessionTransport
from platform_sessions.routes import Route

PUBLIC_OWNER = "p" * 32
PUBLIC_ROUTE = "q" * 32
PRIVATE_OWNER = "o" * 32
PRIVATE_ROUTE = "r" * 32


class Settings(BaseModel):
    pass


class Control(rpc.SessionProviderControlServicer):
    def __init__(self) -> None:
        self.get_info_calls = 0

    @override
    async def GetInfo(self, request, context):
        self.get_info_calls += 1
        return pb.GetInfoResponse(
            provider=pb.ProviderInfo(
                provider_id="example",
                display_name="Example",
                version="test",
                instances=[
                    pb.ProviderInstanceInfo(
                        instance_id="account",
                        instructions=[pb.ExportInstruction(text="Export from Chrome.")],
                        accepted_session_import_formats=[
                            pb.SessionImportFormat(media_type="application/json", version="1")
                        ],
                    )
                ],
            )
        )

    @override
    async def GetSession(self, request, context):
        return pb.GetSessionResponse(
            instance=pb.InstanceRef(provider_id="example", instance_id="account"),
            snapshot=pb.SessionSnapshot(state=pb.SESSION_STATE_NO_SESSION, state_changed_at=Timestamp(seconds=1)),
        )


class LocalSessionTransport:
    def __init__(self) -> None:
        self.requests: list[ProxyRequest] = []

    async def request(self, request: ProxyRequest) -> ProxyResponse:
        self.requests.append(request)
        if request.method == "GET":
            return ProxyResponse(302, ((b"location", b"/api/orders/2"), (b"set-cookie", b"private=rotated")), b"")
        return ProxyResponse(200, ((b"content-type", b"application/octet-stream"),), b"\x00\xff")


def _bridge_to_private(app: ProviderProxy, seen: list[httpx2.Request]):
    def bridge(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)

        async def invoke():
            sent = []
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": request.method,
                "scheme": "https",
                "path": request.url.path,
                "raw_path": request.url.raw_path.split(b"?", 1)[0],
                "query_string": request.url.query,
                "headers": request.headers.raw,
                "client": ("test", 1),
                "server": ("provider.internal", 443),
                "root_path": "",
            }

            async def receive():
                return {"type": "http.request", "body": request.content, "more_body": False}

            async def send(message):
                sent.append(message)

            await app(scope, receive, send)
            return sent

        start, body = asyncio.run(invoke())
        return httpx2.Response(start["status"], headers=start["headers"], stream=httpx2.ByteStream(body["body"]))

    return httpx2.MockTransport(bridge)


@asynccontextmanager
async def served(tmp_path: Path):
    control = Control()
    server = grpc.aio.server()
    rpc.add_SessionProviderControlServicer_to_server(control, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}", options=(("grpc.enable_retries", 0),))
    token_file = tmp_path / "control-bearer"
    token_file.write_text("c" * 32)
    config = IntegrationInstance(
        "account", "example", True, "https://upstream.test/api", PurePosixPath("/data/account"), 900, Settings()
    )
    spec = RemoteProviderSpec(
        config=config,
        provider_origin="https://provider.internal",
        control_target=f"127.0.0.1:{port}",
        control_token_file=token_file,
        control_timeout_s=2,
        owner_token=SecretStr(PRIVATE_OWNER),
        route_token=SecretStr(PRIVATE_ROUTE),
    )
    try:
        yield control, channel, spec
    finally:
        await channel.close()
        await server.stop(grace=0)


@pytest.mark.asyncio
async def test_binding_preserves_gateway_auth_and_private_identity(tmp_path):
    async with served(tmp_path) as (_control, channel, spec):
        tls_context = ssl.create_default_context()
        configured_spec = replace(spec, tls_context=tls_context)
        local = LocalSessionTransport()
        private_auth = BearerAuth(
            [
                Bearer(SecretStr(PRIVATE_OWNER), owner=True),
                Bearer(SecretStr(PRIVATE_ROUTE), routes={"account": (Route("GET", "/orders/*"),)}),
            ]
        )
        private = ProviderProxy({"account": ProxyInstance(configured_spec.config, local)}, private_auth)
        private_requests: list[httpx2.Request] = []
        binding = bind_remote_provider(
            configured_spec, control_channel=channel, http_transport=_bridge_to_private(private, private_requests)
        )
        assert isinstance(binding.proxy_instance.transport, RemoteSessionTransport)
        assert binding.proxy_instance.transport.tls_context is tls_context
        public_auth = BearerAuth(
            [
                Bearer(SecretStr(PUBLIC_OWNER), owner=True),
                Bearer(SecretStr(PUBLIC_ROUTE), routes={"account": (Route("GET", "/orders/*"),)}),
            ]
        )
        app = create_shell()
        assert (await binding.control.get_info()).provider.instances[0].instance_id == "account"
        install_proxy(app, {"account": binding.proxy_instance}, public_auth)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as client:
            assert (
                await client.post("/proxy/account/orders/1", headers={"authorization": f"Bearer {PUBLIC_ROUTE}"})
            ).status_code == 403
            assert private_requests == []
            routed = await client.get(
                "/proxy/account/orders/1?x=%2f&x=2",
                headers={"authorization": f"Bearer {PUBLIC_ROUTE}"},
                follow_redirects=False,
            )
            assert (routed.status_code, routed.headers["location"]) == (302, "/proxy/account/orders/2")
            assert "set-cookie" not in routed.headers
            assert private_requests[-1].headers["authorization"] == f"Bearer {PRIVATE_ROUTE}"
            assert private_requests[-1].url.raw_path.endswith(b"/orders/1?x=%2f&x=2")
            assert local.requests[-1].query == b"x=%2f&x=2"
            assert not local.requests[-1].owner

            owned = await client.delete("/proxy/account/other", headers={"authorization": f"Bearer {PUBLIC_OWNER}"})
            assert (owned.status_code, owned.content) == (200, b"\x00\xff")
            assert private_requests[-1].headers["authorization"] == f"Bearer {PRIVATE_OWNER}"
            assert local.requests[-1].owner
            assert local.requests[-1].url == "https://upstream.test/api/other"


@pytest.mark.asyncio
async def test_binding_lifecycle_and_fail_closed_configuration(tmp_path, monkeypatch):
    async with served(tmp_path) as (control, borrowed_channel, spec):
        unverified_context = ssl.create_default_context()
        unverified_context.check_hostname = False
        with pytest.raises(ValueError, match="verify certificates and hostnames"):
            bind_remote_provider(replace(spec, tls_context=unverified_context), control_channel=borrowed_channel)
        with pytest.raises(ValueError, match="distinct"):
            bind_remote_provider(
                RemoteProviderSpec(**{**spec.__dict__, "route_token": SecretStr(PRIVATE_OWNER)}),
                control_channel=borrowed_channel,
            )
        with pytest.raises(ValueError, match="HTTPS"):
            bind_remote_provider(
                RemoteProviderSpec(**{**spec.__dict__, "provider_origin": "http://provider.internal"}),
                control_channel=borrowed_channel,
            )

        async with open_remote_provider(spec, control_channel=borrowed_channel) as binding:
            assert (await binding.control.get_info()).provider.instances[0].instance_id == "account"
        # Borrowing leaves the control channel usable for the caller.
        assert (await binding.control.get_info()).provider.instances[0].instance_id == "account"

        opened: list[grpc.aio.Channel] = []

        def dial(target, *, credentials, options):
            assert target == spec.control_target
            assert options == (("grpc.keepalive_time_ms", 30_000),)
            channel = grpc.aio.insecure_channel(target, options=(("grpc.enable_retries", 0),))
            opened.append(channel)
            return channel

        monkeypatch.setattr("platform_sessions.provider.binding.dial_provider_control", dial)
        async with open_remote_provider(spec, control_options=(("grpc.keepalive_time_ms", 30_000),)) as owned:
            assert (await owned.control.get_info()).provider.instances[0].instance_id == "account"
        with pytest.raises(grpc.aio.UsageError, match="closed"):
            await owned.control.get_info()
        assert len(opened) == 1
        assert control.get_info_calls == 3


if __name__ == "__main__":
    pytest_bazel.main()
