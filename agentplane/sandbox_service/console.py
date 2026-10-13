"""A VM's serial console, attached through the Kubernetes API's KubeVirt subresource."""

from collections.abc import AsyncIterator

from aiohttp import WSMsgType, WSServerHandshakeError
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.stream import WsApiClient

from agentplane.sandbox_service.errors import ConsoleUnavailableError

CONSOLE_PATH = "/apis/subresources.kubevirt.io/v1/namespaces/{namespace}/virtualmachineinstances/{name}/console"
# KubeVirt's websocket subprotocol for a raw byte stream, without the exec channel framing.
CONSOLE_SUBPROTOCOL = "plain.kubevirt.io"


async def stream_console(
    configuration: k8s_client.Configuration, *, namespace: str, vmi: str
) -> AsyncIterator[bytes]:
    """The console's output from the moment of attach. KubeVirt serves one attached client per VMI at a
    time and replays nothing; nothing is written to the guest."""
    async with WsApiClient(configuration=configuration) as ws_api:
        try:
            websocket = await ws_api.call_api(
                CONSOLE_PATH,
                "GET",
                path_params={"namespace": namespace, "name": vmi},
                header_params={"sec-websocket-protocol": CONSOLE_SUBPROTOCOL},
                auth_settings=["BearerToken"],
                _preload_content=False,
            )
            async with websocket as ws:
                async for message in ws:
                    if message.type in {WSMsgType.BINARY, WSMsgType.TEXT}:
                        yield message.data if isinstance(message.data, bytes) else message.data.encode()
                    elif message.type == WSMsgType.ERROR:
                        raise ConnectionError(f"console of {vmi} failed: {ws.exception()}")
        except WSServerHandshakeError as error:
            raise ConsoleUnavailableError(vmi, status=error.status) from error
