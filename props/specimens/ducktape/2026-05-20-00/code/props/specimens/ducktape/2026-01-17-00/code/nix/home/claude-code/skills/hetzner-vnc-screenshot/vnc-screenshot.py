#!/usr/bin/env -S uv run --with asyncvnc --with pillow --with typer --with hcloud --with websockets
"""
Hetzner Cloud VNC Console Screenshot Tool

Connects to Hetzner's WebSocket-based VNC console and captures a screenshot.

Usage:
    # With server name (requires HCLOUD_TOKEN env var):
    ./vnc-screenshot.py my-server-name -v

    # With explicit credentials:
    ./vnc-screenshot.py --url <wss_url> --password <password>

References:
    - https://docs.hetzner.cloud/#server-actions-request-console-for-a-server
    - https://hcloud-python.readthedocs.io/
    - https://github.com/barneygale/asyncvnc
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Annotated

import asyncvnc
import typer
import websockets
from hcloud import Client
from PIL import Image

logger = logging.getLogger(__name__)


class WebSocketStreamAdapter:
    """Adapts a websocket to provide stream-like readline/write for asyncvnc.

    asyncvnc expects asyncio.StreamReader/StreamWriter-like objects with readline()
    and write() methods, but websockets provides message-based recv()/send().
    """

    def __init__(self, ws):
        self.ws = ws
        self._buffer = bytearray()
        self._pending_write = b""

    async def readline(self) -> bytes:
        """Read until newline, buffering websocket messages as needed."""
        logger.debug("readline() called, waiting for data...")
        while b"\n" not in self._buffer:
            try:
                logger.debug("Waiting for websocket recv()...")
                data = await self.ws.recv()
                logger.debug(f"Received {len(data)} bytes: {data[:50]}...")
                if isinstance(data, str):
                    data = data.encode()
                self._buffer.extend(data)
            except websockets.exceptions.ConnectionClosed as e:
                logger.debug(f"Connection closed: {e}")
                result = bytes(self._buffer)
                self._buffer.clear()
                return result

        idx = self._buffer.index(b"\n") + 1
        result = bytes(self._buffer[:idx])
        del self._buffer[:idx]
        logger.debug(f"readline() returning: {result}")
        return result

    async def read(self, n: int) -> bytes:
        """Read exactly n bytes, buffering websocket messages as needed."""
        while len(self._buffer) < n:
            try:
                data = await self.ws.recv()
                if isinstance(data, str):
                    data = data.encode()
                self._buffer.extend(data)
            except websockets.exceptions.ConnectionClosed:
                break

        result = bytes(self._buffer[:n])
        del self._buffer[:n]
        return result

    async def readexactly(self, n: int) -> bytes:
        """Alias for read() - asyncvnc uses this."""
        return await self.read(n)

    def write(self, data: bytes):
        """Queue data to be sent. Note: asyncvnc doesn't always call drain()."""
        self._pending_write += data
        # Schedule immediate send since asyncvnc often doesn't call drain()
        # Store task reference to prevent garbage collection (RUF006)
        self._send_task = asyncio.create_task(self._send_pending())

    async def _send_pending(self):
        """Send any pending write data."""
        if self._pending_write:
            data = self._pending_write
            self._pending_write = b""
            logger.debug(f"Sending {len(data)} bytes")
            await self.ws.send(data)

    async def drain(self):
        """Compatibility method - data is sent immediately via write()."""


def request_console_credentials(server_name: str, token: str | None = None) -> tuple[str, str]:
    """Request VNC console credentials from Hetzner Cloud API."""
    logger.debug(f"Requesting console for server '{server_name}'")
    if token is None:
        token = os.environ.get("HCLOUD_TOKEN")
        if not token:
            raise ValueError("HCLOUD_TOKEN environment variable not set and no --token provided")
    logger.debug(f"Token length: {len(token)}")

    logger.debug("Creating hcloud Client...")
    client = Client(token=token)
    logger.debug("Fetching servers...")
    servers = client.servers.get_all(name=server_name)
    logger.debug(f"Found {len(servers)} servers")
    if not servers:
        raise ValueError(f"Server '{server_name}' not found")

    logger.debug("Requesting console from Hetzner API...")
    response = client.servers.request_console(servers[0])
    logger.debug(f"Got console URL: {response.wss_url[:50]}...")
    return response.wss_url, response.password


async def vnc_screenshot(wss_url: str, password: str, output_path: str = "screenshot.png"):
    """Connect to VNC over WebSocket and capture a screenshot."""
    logger.debug(f"Connecting to {wss_url[:60]}...")

    logger.debug("Opening websocket connection...")
    async with websockets.connect(wss_url, subprotocols=["binary"]) as ws:
        logger.debug("Websocket connected, creating adapter...")
        adapter = WebSocketStreamAdapter(ws)
        logger.debug("Creating asyncvnc Client...")
        client = await asyncvnc.Client.create(reader=adapter, writer=adapter, password=password)

        logger.info(f"Connected. Screen: {client.video.width}x{client.video.height}")
        logger.debug("Taking screenshot...")
        pixels = await client.screenshot()
        logger.debug("Converting to image...")
        img = Image.fromarray(pixels)
        img.save(output_path)
        logger.info(f"Screenshot saved to {output_path}")


app = typer.Typer(help="Hetzner VNC console screenshot tool")


@app.command()
def main(
    server: Annotated[str | None, typer.Argument(help="Server name (requires HCLOUD_TOKEN env var)")] = None,
    url: Annotated[str | None, typer.Option(help="WebSocket URL (from 'hcloud server request-console')")] = None,
    password: Annotated[str | None, typer.Option(help="VNC password")] = None,
    token: Annotated[str | None, typer.Option(help="Hetzner API token (default: HCLOUD_TOKEN env)")] = None,
    output: Annotated[Path, typer.Option(help="Output image path")] = Path("screenshot.png"),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable debug logging")] = False,
):
    """Capture a screenshot from Hetzner Cloud VNC console.

    Either provide a server name (uses Hetzner API to get console credentials)
    or provide --url and --password explicitly.
    """
    # Configure logging to stderr with appropriate level
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    if server:
        if url or password:
            raise typer.BadParameter("Cannot use --url/--password with server name argument")
        logger.info(f"Fetching console credentials for server '{server}'...")
        wss_url, vnc_password = request_console_credentials(server, token)
        logger.info("Got console credentials")
    elif url and password:
        wss_url, vnc_password = url, password
    else:
        raise typer.BadParameter("Provide either server name or both --url and --password")

    asyncio.run(vnc_screenshot(wss_url, vnc_password, str(output)))


if __name__ == "__main__":
    app()
