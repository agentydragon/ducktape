"""One private, headed Chromium profile and an authenticated VNC WebSocket bridge.

The caller owns the fixed ASGI route and provider-specific session state. Chromium and
Playwright live on one worker thread; ``run_sync`` sends short provider operations to that
thread. The owner sees the same profile through loopback x11vnc and websockify.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import queue
import select
import socket
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Future
from contextlib import suppress
from pathlib import Path
from typing import Any, TypeVar, cast
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Error as PlaywrightError, sync_playwright
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect as connect_websocket
from websockets.exceptions import WebSocketException
from websockets.typing import Subprotocol

logger = logging.getLogger(__name__)
_T = TypeVar("_T")
_MAX_WS_MESSAGE = 1024 * 1024


class BrowserBusy(RuntimeError):  # noqa: N818 - stable caller-facing admission signal
    """A data-plane browser operation was attempted while the owner is viewing it."""


class _Work:
    def __init__(self, callback: Callable[[BrowserContext], Any]):
        self.callback = callback
        self.result: Future[Any] = Future()


class InteractiveBrowserRuntime:
    """Keep an instance's browser alive across owner reconnects and provider operations.

    ``launch_options`` is passed to Playwright after enforcing a headed browser and bounded
    caches. It can carry the provider's existing proxy and executable path. The profile must
    be a per-instance durable directory; credentials in it are never copied to the gateway.
    ``owner_token`` is the gateway-to-provider secret, not the browser owner's OIDC cookie.
    """

    def __init__(
        self,
        *,
        profile_dir: Path,
        initial_url: str,
        owner_token: str,
        launch_options: Mapping[str, Any] | None = None,
        on_viewer_connect: Callable[[], Awaitable[None] | None] | None = None,
        on_viewer_disconnect: Callable[[], Awaitable[None] | None] | None = None,
        xvfb_executable: str = "Xvfb",
        vnc_executable: str = "x11vnc",
        websockify_executable: str = "websockify",
        viewport: tuple[int, int] = (1280, 800),
        startup_timeout_seconds: float = 30.0,
    ) -> None:
        url = urlsplit(initial_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
            raise ValueError("initial_url must be a fixed HTTPS URL without userinfo or fragment")
        if len(owner_token) < 32 or not owner_token.isascii() or any(not 32 < ord(c) < 127 for c in owner_token):
            raise ValueError("owner_token must be at least 32 visible ASCII characters")
        if any(size <= 0 for size in viewport) or startup_timeout_seconds <= 0:
            raise ValueError("browser dimensions and startup timeout must be positive")
        options = dict(launch_options or {})
        if any(key in options for key in ("headless", "env", "viewport", "user_data_dir")):
            raise ValueError("launch_options cannot override browser isolation settings")
        self._profile_dir = profile_dir
        self._initial_url = initial_url
        self._owner_token = owner_token
        self._launch_options = options
        self._on_viewer_connect = on_viewer_connect
        self._on_viewer_disconnect = on_viewer_disconnect
        self._xvfb_executable = xvfb_executable
        self._vnc_executable = vnc_executable
        self._websockify_executable = websockify_executable
        self._viewport = viewport
        self._startup_timeout_seconds = startup_timeout_seconds
        self._jobs: queue.Queue[_Work | None] = queue.Queue()
        self._state_lock = threading.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._state = "created"
        self._viewer_connected = False
        self._pending_data_jobs = 0
        self._viewer_socket: WebSocket | None = None
        self._viewer_ended = asyncio.Event()
        self._viewer_ended.set()
        self._thread: threading.Thread | None = None
        self._context: BrowserContext | None = None
        self._xvfb: subprocess.Popen[bytes] | None = None
        self._vnc: subprocess.Popen[bytes] | None = None
        self._websockify: subprocess.Popen[bytes] | None = None
        self._display: str | None = None
        self._vnc_port: int | None = None
        self._websockify_port: int | None = None

    @property
    def viewer_connected(self) -> bool:
        with self._state_lock:
            return self._viewer_connected

    async def start(self) -> None:
        """Start Xvfb, loopback VNC and one persistent headed Chromium context."""
        async with self._lifecycle_lock:
            with self._state_lock:
                if self._state == "running":
                    return
                if self._state != "created":
                    raise RuntimeError("browser runtime cannot be restarted")
                self._state = "starting"
            try:
                await asyncio.to_thread(self._start_blocking)
            except BaseException:
                await asyncio.to_thread(self._stop_blocking)
                with self._state_lock:
                    self._state = "stopped"
                raise
            with self._state_lock:
                self._state = "running"

    def run_sync(self, callback: Callable[[BrowserContext], _T], *, allow_during_viewer: bool = False) -> _T:
        """Run a short provider callback on Playwright's owner thread.

        Data-plane work is refused while the owner views the browser. Finishing sign-in can
        opt in so the provider may verify the browser while the owner is still connected.
        """
        with self._state_lock:
            if self._state != "running":
                raise RuntimeError("browser runtime is not running")
            if self._thread is None or not self._thread.is_alive():
                raise RuntimeError("browser worker has stopped")
            if self._viewer_connected and not allow_during_viewer:
                raise BrowserBusy("owner is using the browser")
            work = _Work(callback)
            if not allow_during_viewer:
                self._pending_data_jobs += 1
            self._jobs.put(work)
        try:
            return cast("_T", work.result.result())
        finally:
            if not allow_during_viewer:
                with self._state_lock:
                    self._pending_data_jobs -= 1

    async def websocket(self, websocket: WebSocket) -> None:
        """Authenticate and relay one local websockify connection."""
        supplied = websocket.headers.get("authorization", "")
        if not supplied.isascii() or not hmac.compare_digest(supplied, "Bearer " + self._owner_token):
            await websocket.close(code=4401)
            return
        with self._state_lock:
            if self._state != "running" or self._websockify_port is None:
                reject = 1013
            elif self._viewer_connected:
                reject = 4409
            elif self._pending_data_jobs:
                reject = 1013
            else:
                reject = None
                self._viewer_connected = True
                self._viewer_socket = websocket
                port = self._websockify_port
                self._viewer_ended.clear()
        if reject is not None:
            await websocket.close(code=reject)
            return

        try:
            if self._on_viewer_connect is not None:
                completion = self._on_viewer_connect()
                if asyncio.iscoroutine(completion):
                    await completion
            async with connect_websocket(
                f"ws://127.0.0.1:{port}/", subprotocols=[Subprotocol("binary")], max_size=_MAX_WS_MESSAGE
            ) as local:
                subprotocol = "binary" if "binary" in websocket.scope.get("subprotocols", ()) else None
                await websocket.accept(subprotocol=subprotocol)
                upstream = asyncio.create_task(self._local_to_websocket(local, websocket))
                downstream = asyncio.create_task(self._websocket_to_local(websocket, local))
                done, pending = await asyncio.wait((upstream, downstream), return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    task.result()
        except (OSError, RuntimeError, WebSocketDisconnect, WebSocketException) as error:
            logger.warning("interactive browser connection ended error_type=%s", type(error).__name__)
        except Exception as error:
            logger.error(  # noqa: TRY400 - exception text may contain credentials
                "interactive browser connection failed error_type=%s", type(error).__name__
            )
            with suppress(OSError, RuntimeError):
                await websocket.close(code=1011)
        finally:
            with self._state_lock:
                self._viewer_connected = False
                self._viewer_socket = None
            try:
                if self._on_viewer_disconnect is not None:
                    completion = self._on_viewer_disconnect()
                    if asyncio.iscoroutine(completion):
                        await completion
            except Exception as error:
                logger.error(  # noqa: TRY400 - exception text may contain credentials
                    "interactive browser disconnect callback failed error_type=%s", type(error).__name__
                )
            self._viewer_ended.set()

    async def close(self) -> None:
        """Stop admitting browser work and disconnect the viewer before process teardown."""
        async with self._lifecycle_lock:
            with self._state_lock:
                if self._state == "stopped":
                    return
                self._state = "stopping"
                viewer = self._viewer_socket
            if viewer is not None:
                with suppress(OSError, RuntimeError):
                    await viewer.close(code=1001)
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._viewer_ended.wait(), timeout=5)
            await asyncio.to_thread(self._stop_blocking)
            with self._state_lock:
                self._state = "stopped"

    async def _local_to_websocket(self, local: Any, websocket: WebSocket) -> None:
        async for message in local:
            if isinstance(message, str):
                await websocket.send_text(message)
            else:
                await websocket.send_bytes(message)

    async def _websocket_to_local(self, websocket: WebSocket, local: Any) -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            payload = message.get("bytes", message.get("text"))
            if payload is None or len(payload) > _MAX_WS_MESSAGE:
                await websocket.close(code=1003)
                return
            await local.send(payload)

    def _start_blocking(self) -> None:
        self._profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._profile_dir.chmod(0o700)
        width, height = self._viewport
        self._xvfb = subprocess.Popen(
            [
                self._xvfb_executable,
                "-displayfd",
                "1",
                "-screen",
                "0",
                f"{width}x{height}x24",
                "-nolisten",
                "tcp",
                "-noreset",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert self._xvfb.stdout is not None
        ready, _, _ = select.select([self._xvfb.stdout], [], [], self._startup_timeout_seconds)
        if not ready:
            raise TimeoutError("virtual display did not start")
        display_number = self._xvfb.stdout.readline().strip().decode("ascii", errors="ignore")
        if not display_number.isdecimal() or self._xvfb.poll() is not None:
            raise RuntimeError("virtual display did not start")
        self._display = f":{display_number}"
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            self._vnc_port = reserved.getsockname()[1]
        self._vnc = subprocess.Popen(
            [
                self._vnc_executable,
                "-display",
                self._display,
                "-localhost",
                "-nopw",
                "-rfbport",
                str(self._vnc_port),
                "-forever",
                "-shared",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + self._startup_timeout_seconds
        while time.monotonic() < deadline:
            if self._vnc.poll() is not None:
                raise RuntimeError("VNC server did not start")
            try:
                with socket.create_connection(("127.0.0.1", self._vnc_port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise TimeoutError("VNC server did not start")

        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            self._websockify_port = reserved.getsockname()[1]
        self._websockify = subprocess.Popen(
            [self._websockify_executable, f"127.0.0.1:{self._websockify_port}", f"127.0.0.1:{self._vnc_port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + self._startup_timeout_seconds
        while time.monotonic() < deadline:
            if self._websockify.poll() is not None:
                raise RuntimeError("websockify did not start")
            try:
                with socket.create_connection(("127.0.0.1", self._websockify_port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise TimeoutError("websockify did not start")

        started = threading.Event()
        failure: list[BaseException] = []

        def serve() -> None:
            try:
                with sync_playwright() as playwright:
                    options = dict(self._launch_options)
                    args = list(options.pop("args", ()))
                    args.extend(
                        (
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            "--disk-cache-size=16777216",
                            "--media-cache-size=8388608",
                        )
                    )
                    context = playwright.chromium.launch_persistent_context(
                        str(self._profile_dir),
                        headless=False,
                        viewport=None,
                        args=args,
                        env={**os.environ, "DISPLAY": self._display or ""},
                        timeout=self._startup_timeout_seconds * 1000,
                        **options,
                    )
                    self._context = context
                    try:
                        if not context.pages or all(page.url == "about:blank" for page in context.pages):
                            page = context.pages[0] if context.pages else context.new_page()
                            try:
                                page.goto(self._initial_url, wait_until="commit", timeout=15000)
                            except PlaywrightError as error:
                                logger.warning("initial sign-in navigation failed error_type=%s", type(error).__name__)
                        started.set()
                        while (work := self._jobs.get()) is not None:
                            try:
                                work.result.set_result(work.callback(context))
                            except BaseException as error:
                                work.result.set_exception(error)
                    finally:
                        context.close()
                        self._context = None
            except BaseException as error:
                failure.append(error)
                started.set()
            finally:
                while True:
                    try:
                        unfinished = self._jobs.get_nowait()
                    except queue.Empty:
                        break
                    if unfinished is not None and not unfinished.result.done():
                        unfinished.result.set_exception(RuntimeError("browser worker stopped"))

        self._thread = threading.Thread(target=serve, name="interactive-browser", daemon=True)
        self._thread.start()
        if not started.wait(self._startup_timeout_seconds + 16):
            raise TimeoutError("Chromium did not start")
        if failure:
            raise RuntimeError("Chromium did not start") from failure[0]

    def _stop_blocking(self) -> None:
        if self._thread is not None:
            self._jobs.put(None)
            self._thread.join(timeout=15)
            if self._thread.is_alive():
                logger.error("interactive Chromium worker did not stop before timeout")
            self._thread = None
        for process in (self._websockify, self._vnc, self._xvfb):
            if process is None:
                continue
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            if process.stdout is not None:
                process.stdout.close()
        self._vnc = None
        self._websockify = None
        self._xvfb = None
        self._vnc_port = None
        self._websockify_port = None
        self._display = None
