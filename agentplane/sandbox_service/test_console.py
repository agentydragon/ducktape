from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_bazel
from aiohttp import web
from aiohttp.test_utils import TestServer
from kubernetes_asyncio import client as k8s_client

from agentplane.sandbox_service.console import CONSOLE_SUBPROTOCOL, stream_console
from agentplane.sandbox_service.errors import ConsoleUnavailableError


@asynccontextmanager
async def kubernetes_api(app: web.Application) -> AsyncIterator[k8s_client.Configuration]:
    async with TestServer(app) as server:
        configuration = k8s_client.Configuration(host=str(server.make_url("")).rstrip("/"))
        configuration.api_key = {"BearerToken": "test-token"}
        configuration.api_key_prefix = {"BearerToken": "Bearer"}
        yield configuration


async def test_streams_the_console_with_the_service_credentials() -> None:
    seen: dict[str, str] = {}

    async def console(request: web.Request) -> web.WebSocketResponse:
        seen.update(path=request.path, authorization=request.headers["Authorization"])
        ws = web.WebSocketResponse(protocols=(CONSOLE_SUBPROTOCOL,))
        await ws.prepare(request)
        await ws.send_bytes(b"boot ")
        await ws.send_bytes(b"ok\r\n")
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get(
        "/apis/subresources.kubevirt.io/v1/namespaces/{namespace}/virtualmachineinstances/{vmi}/console", console
    )
    async with kubernetes_api(app) as configuration:
        output = [chunk async for chunk in stream_console(configuration, namespace="sandboxes-test", vmi="vm-test")]

    assert b"".join(output) == b"boot ok\r\n"
    assert seen == {
        "path": "/apis/subresources.kubevirt.io/v1/namespaces/sandboxes-test/virtualmachineinstances/vm-test/console",
        "authorization": "Bearer test-token",
    }


@pytest.mark.parametrize("status", [web.HTTPBadRequest, web.HTTPNotFound])
async def test_a_vm_that_cannot_be_attached_is_unavailable(status: type[web.HTTPException]) -> None:
    async def refuse(request: web.Request) -> web.Response:
        raise status()

    app = web.Application()
    app.router.add_get(
        "/apis/subresources.kubevirt.io/v1/namespaces/{namespace}/virtualmachineinstances/{vmi}/console", refuse
    )
    async with kubernetes_api(app) as configuration:
        with pytest.raises(ConsoleUnavailableError):
            [chunk async for chunk in stream_console(configuration, namespace="sandboxes-test", vmi="vm-test")]


if __name__ == "__main__":
    pytest_bazel.main()
