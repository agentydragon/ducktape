"""TLS/HTTP2 browser ingress for concurrent Electric shape requests in acceptance tests."""

import asyncio
import ssl
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass

import httpx
from jinja2 import Template
from python.runfiles import Runfiles

from agentplane.app.frontend.serving.testing.image import LAYOUT_RLOCATION
from agentplane.app.testing.browser_certificate import BrowserCertificate
from devinfra.oci.oci import load_oci_image
from third_party.containers import nginx_unprivileged
from util.testing.container_logs import LoggedContainer

# HTTPX loads its optional HTTP/2 transport lazily.
# gazelle:include_dep @pypi//h2


@dataclass(frozen=True)
class Ingress:
    url: str
    container: LoggedContainer

    async def drop_connections(self) -> None:
        """End every open connection, as a network drop does, while still accepting new ones.

        Browser offline emulation fails requests still waiting for a response, but not one
        already streaming, such as a live shape's SSE response.
        """
        exit_code, output = await asyncio.to_thread(self.container.exec, "nginx -c /test/nginx.conf -s reload")
        assert exit_code == 0, output


async def _wait_for_frontend(upstream: str) -> None:
    async with asyncio.timeout(30), httpx.AsyncClient(timeout=2) as client:
        while True:
            try:
                response = await client.get(f"{upstream}/healthz")
            # Docker can publish the port before nginx can answer: connections may be
            # refused, reset, or closed before HTTP response headers arrive.
            except httpx.TransportError:
                await asyncio.sleep(0.1)
                continue
            response.raise_for_status()
            return


@asynccontextmanager
async def http2_proxy(upstream: str, certificate: BrowserCertificate) -> AsyncIterator[Ingress]:
    ingress_image = await asyncio.to_thread(load_oci_image, nginx_unprivileged.LAYOUT_RLOCATION)
    static_image = await asyncio.to_thread(load_oci_image, LAYOUT_RLOCATION)
    with ExitStack() as stack:
        static_container = stack.enter_context(
            LoggedContainer(static_image, test_name="conversation-static-frontend").with_exposed_ports(8080)
        )
        static_upstream = f"http://{static_container.get_container_host_ip()}:{static_container.get_exposed_port(8080)}"
        await _wait_for_frontend(static_upstream)
        # A separate nginx serves the actual built frontend. The TLS ingress supplies the
        # browser's single origin while API traffic still reaches the real app process.
        port = certificate.port
        template = (Runfiles.CreateOrRaise().root() / "ducktape/agentplane/app/testing/http2_proxy.conf.j2").read_text()
        (certificate.directory / "nginx.conf").write_text(
            Template(template).render(port=port, upstream=upstream, static_upstream=static_upstream) + "\n"
        )
        container = (
            LoggedContainer(ingress_image, test_name="conversation-http2-ingress")
            .with_kwargs(network_mode="host")
            .with_volume_mapping(str(certificate.directory), "/test", mode="ro")
            .with_command("nginx -c /test/nginx.conf -g 'daemon off;'")
        )
        url = f"https://127.0.0.1:{port}"
        context = ssl.create_default_context(cafile=str(certificate.directory / "certificate.pem"))
        certificate.port_reservation.close()
        with container:
            async with asyncio.timeout(30), httpx.AsyncClient(verify=context, http2=True, timeout=2) as client:
                while True:
                    try:
                        response = await client.get(f"{url}/readyz")
                    # A slow first answer is not a failed start: the 30 s budget decides that.
                    except httpx.ConnectError, httpx.TimeoutException:
                        await asyncio.sleep(0.1)
                        continue
                    response.raise_for_status()
                    assert response.http_version == "HTTP/2"
                    break
            yield Ingress(url, container)
